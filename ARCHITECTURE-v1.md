# chameleon — Architecture v1

> *"Code that blends in."*

> **Status:** v1 draft for multi-agent review (Phase 1 of design process)
> **Date:** 2026-05-10
> **Author:** Cris Nahine + Claude (drafted via Phase 0 superpowers conventions scan)
> **Successor relationship:** Generic engine that supersedes the EF-specific `claude-measure-twice`. The latter becomes a profile pack overlay in this design.

---

## Purpose

A Claude Code plugin that gives the AI deep, persistent understanding of any codebase's conventions — so AI-generated code lands closer to merge-ready on the first try, regardless of language, framework, or team.

Reviewers focus on logic, security, and tests rather than file shape, naming, or idiom usage.

**Target:** ~95% pattern conformance on archetype-matched tasks across any repo, at <$0.40 per steady-state session.

---

## Goals

1. **Universal applicability** — works on any repo (any language, any framework, any team)
2. **Single install, multi-repo** — install once; plugin handles every repo on the dev's machine
3. **Auto-onboarding** — first time in a new repo, plugin offers to bootstrap a profile (no per-repo manual setup beyond an explicit `/chameleon-init`)
4. **Co-existence** — works alongside superpowers and any other Claude Code plugin
5. **Pre-built profile packs** — teams can ship hand-curated profiles for their organization's repos
6. **Cost ceiling** — bootstrap acceptable high (one-time investment), steady-state <$0.40/session, <$50/month per developer
7. **Superpowers-style discipline** — file/skill format, hook patterns, bootstrap mechanism mirror superpowers' battle-tested approach

---

## Plugin name: `chameleon`

> *Tagline: "Code that blends in."*

A chameleon adapts coloring to its environment without losing its identity. Same with this plugin: Claude remains Claude underneath, but the AI's output adapts to blend with each repo's existing style — file shape, naming, idioms, architecture. The metaphor pairs naturally with superpowers ("you have superpowers and chameleon"), evokes the right mental model immediately, and captures the core function (adaptive blending without identity loss).

**Alternatives considered:**

| Name | Why considered | Why not chosen |
|---|---|---|
| `apprentice` | Studies masters before producing work; humble + respectful | Too student-coded; doesn't capture adaptation |
| `mockingbird` | Bird that learns local songs; literary weight | Minor collision with "mocks" in testing terminology |
| `journeyman` | Craftsperson who learns each shop's methods; lineage from "measure twice" | Heavier/older feel; less immediate metaphor |
| `native` | Implies fluency, belonging | Heavily overloaded in tech (native code, React Native) |
| `claude-measure` | Direct evolution from `claude-measure-twice` | Too literal; doesn't carry a metaphor |

**Conventions following from the name:**
- Plugin/repo name: `chameleon` (no `claude-` prefix; mirrors `superpowers`)
- Slash command prefix: `/chameleon-*` (e.g., `/chameleon-init`)
- Skill prefix: `chameleon-*` (e.g., `chameleon-init`)
- Foundation skill: `using-chameleon`
- MCP server: `chameleon-mcp`
- Python package: `chameleon_mcp`
- Profile dir: `.chameleon/profile.json`
- Env var prefix: `CHAMELEON_*`

---

## Core principles

1. **Foundation generic, brain per-repo.** The engine ships with no repo-specific knowledge. Profiles supply that, generated per repo.
2. **Profile is a portable artifact.** Committed JSON + Markdown, version-controllable, team-shareable.
3. **Two-tier dimensions.** Auto-derivable (AST + statistical analysis) vs hand-curated (`idioms.md` escape hatch for what AST cannot infer).
4. **Discovery before action.** Every code edit injects archetype context before the model writes.
5. **Inject context, don't deny.** Only the security layer hard-denies; conformance is advisory.
6. **Plugin coexistence first-class.** Namespaced everything, path-scoped Skills, token budget discipline.
7. **Superpowers conventions.** File layout, skill format, hook patterns, bootstrap mechanism mirror superpowers' battle-tested approach.

---

## High-level architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                       chameleon (engine)                            │
│                                                                          │
│  ┌──────────────────────────┐     ┌──────────────────────────────────┐ │
│  │ Hooks                    │     │ Skills                           │ │
│  │ ─────                    │     │ ──────                           │ │
│  │ SessionStart             │     │ using-chameleon (bootstrap)        │ │
│  │  → session-start         │     │ chameleon-init (slash command)     │ │
│  │  → inject using-chameleon  │     │ chameleon-refresh                  │ │
│  │  + profile primer        │     │ chameleon-refine                   │ │
│  │ PreToolUse Edit/Write    │     │ chameleon-doctor                   │ │
│  │  → preflight (safety)    │     │ chameleon-profile                  │ │
│  │  → archetype-advisor     │     │ chameleon-apply-pack               │ │
│  │    (mcp_tool)            │     │ chameleon-status                   │ │
│  │ PostToolUse Write        │     │                                  │ │
│  │  → write-fix             │     │ Dynamic archetype skills         │ │
│  │ PostToolUse Bash         │     │  (registered from active profile,│ │
│  │  → posttool-recorder     │     │   auto-load via paths: glob)     │ │
│  │ UserPromptSubmit         │     │                                  │ │
│  │  → callout-detector      │     │                                  │ │
│  └──────────────┬───────────┘     └──────────────┬───────────────────┘ │
│                 └────────────────┬────────────────┘                    │
│                                  ▼                                     │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │                   MCP Server (chameleon-mcp)                       │  │
│  │  detect_repo            get_archetype       lint_file            │  │
│  │  get_canonical_excerpt  get_rules           get_drift_status     │  │
│  │  refresh_repo           bootstrap_repo      list_profiles        │  │
│  │  apply_profile_pack     refine_profile                           │  │
│  └─────────────┬───────────────────────────────┬───────────────────┘  │
│                │                               │                      │
│                ▼                               ▼                      │
│  ┌──────────────────────────────┐  ┌─────────────────────────────────┐│
│  │ Profile storage              │  │ Bootstrap engine                ││
│  │ ────────────────             │  │ ────────────────                ││
│  │ Priority order:              │  │ 1. Detect lang/framework        ││
│  │  1. <repo>/.chameleon/         │  │ 2. AST scan + path cluster      ││
│  │     profile.json (team)      │  │ 3. Statistical pattern extract  ││
│  │  2. ${PLUGIN_DATA}/profiles/ │  │ 4. Archetype proposal           ││
│  │     <repo_id>/  (per-user)   │  │ 5. Interactive confirmation     ││
│  │  3. Profile pack match       │  │ 6. idioms.md user prompt        ││
│  │     (EF, etc.)               │  │ 7. Save (in-repo OR cache)      ││
│  └──────────────────────────────┘  └─────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│           Multi-language AST extractor layer (extensible)                │
│  ruby (Prism) │ typescript (TS Compiler) │ python (libcst)               │
│  ── v1.5 stretch: go (go/parser), rust (syn), php (nikic), java          │
│                                                                          │
│  Each provides: parse_repo, extract_archetypes, extract_patterns         │
└──────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│           Profile Pack Registry (built-in + companion-plugin)            │
│  ────────────────────────────────────────────────────────────────────    │
│  Match by:                                                               │
│   1. Git remote URL exact match                                          │
│   2. Signature heuristics (Gemfile/package.json/file-structure markers)  │
│   3. User explicit: /chameleon-apply-pack <name>                           │
│                                                                          │
│  EF migration: claude-measure-twice → packs/empire-flippers-{api,client} │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Plugin structure (mirrors superpowers conventions)

```
chameleon/
├── .claude-plugin/
│   ├── plugin.json              # name/version/author/keywords (minimal)
│   └── marketplace.json         # self-distribution manifest
├── .codex-plugin/               # Codex harness bootstrap (v1.5)
├── .cursor-plugin/              # Cursor harness bootstrap (v1.5)
├── .opencode/                   # OpenCode harness bootstrap (v1.5)
├── gemini-extension.json        # Gemini extension manifest (v1.5)
├── CLAUDE.md                    # AI agent guidelines
├── AGENTS.md                    # symlink → CLAUDE.md
├── GEMINI.md                    # Gemini-specific notes (v1.5)
├── README.md
├── CHANGELOG.md
├── RELEASE-NOTES.md
├── LICENSE
├── package.json                 # version anchor
├── hooks/
│   ├── hooks.json               # hook manifest (Claude Code)
│   ├── hooks-cursor.json        # Cursor variant
│   ├── run-hook.cmd             # cross-platform polyglot wrapper
│   ├── session-start            # SessionStart hook (extensionless)
│   ├── preflight-check          # PreToolUse Edit|Write safety + advisor
│   ├── posttool-write-fix       # PostToolUse Write retroactive injection
│   ├── posttool-recorder        # PostToolUse Bash exec log (HMAC signed)
│   └── callout-detector         # UserPromptSubmit frustration phrase reminder
├── skills/
│   ├── using-chameleon/           # bootstrap skill (loaded by SessionStart)
│   │   └── SKILL.md
│   ├── chameleon-init/
│   │   └── SKILL.md
│   ├── chameleon-refresh/
│   │   └── SKILL.md
│   ├── chameleon-refine/
│   │   └── SKILL.md
│   ├── chameleon-doctor/
│   │   └── SKILL.md
│   ├── chameleon-profile/
│   │   └── SKILL.md
│   ├── chameleon-apply-pack/
│   │   └── SKILL.md
│   └── chameleon-status/
│       └── SKILL.md
│   # (dynamically registered archetype skills via active profile)
├── mcp/
│   ├── pyproject.toml
│   └── chameleon_mcp/
│       ├── server.py            # FastMCP entry
│       ├── tools/               # MCP tool implementations
│       ├── extractors/          # multi-language AST analysis
│       │   ├── _base.py         # extractor protocol
│       │   ├── ruby.py          # via Prism subprocess
│       │   ├── typescript.py    # via TS Compiler API subprocess
│       │   └── python.py        # via libcst
│       ├── bootstrap/           # interview-driven profile generation
│       ├── profile/             # profile schema + persistence
│       ├── packs/               # built-in profile pack loader
│       └── drift/               # mtime/sha tracking, drift detection
├── scripts/                     # native parser shims
│   ├── prism_dump.rb
│   ├── ts_dump.mjs
│   ├── bump-version.sh
│   ├── sync-to-codex-plugin.sh  # (v1.5)
│   └── sync-to-cursor-plugin.sh # (v1.5)
├── packs/                       # bundled profile packs
│   └── (each pack is a directory of profile artifacts)
├── tests/
│   ├── skill-triggering/        # behavioral evals (mirroring superpowers)
│   ├── unit/                    # extractors, MCP tools, hooks
│   ├── integration/             # end-to-end bootstrap + steady-state flows
│   └── corpus/                  # benchmark repos for conformance testing
├── docs/
│   └── chameleon/
│       ├── specs/               # design specs
│       ├── plans/               # implementation plans
│       └── reference/           # user-facing reference
└── assets/                      # diagrams, logos
```

---

## Bootstrap mechanism (mirrors `using-superpowers` pattern)

The pivotal pattern adopted from superpowers: **SessionStart injects a foundational skill that gates all subsequent behavior.**

```
SessionStart hook fires (matcher: startup|clear|compact)
  → run-hook.cmd session-start
  → bash script:
      1. Read skills/using-chameleon/SKILL.md
      2. Detect active repo:
           - file-path walk-up if a file context is available
           - else cwd as fallback
      3. Check profile state:
           - <repo>/.chameleon/profile.json present?     → load summary
           - per-user cache populated for repo_id?     → load summary
           - profile pack match (remote URL/signature)? → suggest /chameleon-apply-pack
           - none of the above?                         → suggest /chameleon-init
      4. Inject as additionalContext (per-harness JSON format dispatch):
           Wrap in <EXTREMELY_IMPORTANT> tags
           Content: using-chameleon SKILL.md body + repo+profile primer
```

**Per-harness JSON format dispatch** (mirroring superpowers' `session-start` script):
- Cursor: `{ "additional_context": "..." }`
- Claude Code: `{ "hookSpecificOutput": { "hookEventName": "SessionStart", "additionalContext": "..." } }`
- SDK / Copilot CLI: `{ "additionalContext": "..." }`

**The `using-chameleon` skill** (analogous to `using-superpowers`):
- Establishes the system at session start
- Mandates MCP tool calls (`detect_repo`, `get_archetype`, `get_canonical_excerpt`) before any code edit in the active repo
- Lists available slash commands
- Lists Red Flags — rationalizations agents make to skip pattern conformance ("this is just a small fix", "I know the pattern", "this file is too unique")
- Cross-references the per-archetype skills

---

## Hook stack

```
SessionStart (matcher: startup|clear|compact):
  1. session-start
       Inject using-chameleon + repo profile primer
       Budget: ~500-800 tokens

PreToolUse (matcher: Edit|Write|NotebookEdit):
  1. preflight-check
       Safety hard-denies (path traversal, secrets, lockfiles, vendored code, generated files)
       Source classifier (test|config|source|generated)
       Allow/block decision
       (Hard-deny portion inherited verbatim from claude-measure-twice — security-load-bearing)
  2. archetype-advisor (type: "mcp_tool", native in Claude Code 2.1.138+)
       Calls chameleon-mcp::get_canonical_excerpt for the file's archetype
       Injects 500-800 token annotated excerpt as additionalContext

PostToolUse (matcher: Write):
  1. posttool-write-fix
       Compensates Skills paths: gap on Write events
       Calls MCP get_canonical_excerpt + get_rules retroactively
       Model sees on next turn for revision

PostToolUse (matcher: Bash):
  1. posttool-recorder
       HMAC-signed exit code log (debugging aid; tokenless local)

UserPromptSubmit:
  1. callout-detector
       Frustration phrase → rule-update-first reminder

(Stop event — optional v1.5: background drift fingerprint diff, tokenless local compute)
```

---

## Skill design

### Foundation skill: `using-chameleon`

```yaml
---
name: using-chameleon
description: Use when starting any conversation in a repo with a measure profile - establishes pattern-conformance discipline and required MCP tool invocations before code edits
---
```

Body sections (mirroring superpowers' `using-superpowers`):
- `<EXTREMELY-IMPORTANT>` block: pattern conformance is mandatory if profile is present
- `<SUBAGENT-STOP>` block: subagents skip this skill
- The Rule: invoke `detect_repo` + `get_canonical_excerpt` BEFORE editing in profiled repos
- Process flowchart (graphviz `dot`): when to call MCP tools, when to load archetype skills
- Red Flags table: rationalizations to skip pattern conformance
- Available slash commands
- Profile state interpretation

### User-invokable skills (slash commands)

| Skill | Slash command | Purpose |
|---|---|---|
| `chameleon-init` | `/chameleon-init` | Bootstrap a new repo profile via interactive interview |
| `chameleon-refresh` | `/chameleon-refresh` | Re-analyze repo, detect drift, update profile |
| `chameleon-refine` | `/chameleon-refine` | Iterate on profile based on observed misses or reviewer feedback |
| `chameleon-doctor` | `/chameleon-doctor` | Diagnose plugin health, profile state, hook firing |
| `chameleon-profile` | `/chameleon-profile` | Show current active profile (archetypes, rules summary) |
| `chameleon-apply-pack` | `/chameleon-apply-pack <name>` | Apply a pre-built profile pack |
| `chameleon-status` | `/chameleon-status` | Show drift/stats per known repo |

(Slash commands implemented as Skills with `disable-model-invocation: true`, mirroring superpowers' approach.)

### Dynamic archetype skills

When a repo's profile is loaded, the engine dynamically registers per-archetype skills that auto-trigger on path match.

Example: profile declares a `controller` archetype with paths `app/controllers/api/v1/**/*.rb`. The engine generates an ephemeral skill at `${PLUGIN_DATA}/runtime-skills/<repo_id>/chameleon-archetype-controller/SKILL.md` with frontmatter:

```yaml
---
name: chameleon-archetype-controller
description: Use when reading or editing files matching app/controllers/api/v1/**/*.rb - delivers archetype-specific rules and canonical excerpt
paths: ["app/controllers/api/v1/**/*.rb"]
---
```

Body links to the canonical excerpt + rules + idiom notes, all retrieved via MCP at load time.

(The `paths:` mechanism auto-loads on Read/Edit. The PostToolUse Write hook compensates the known gap on Write events.)

---

## MCP server (`chameleon-mcp`)

FastMCP-based, stdio transport. Tools (model-facing):

| Tool | Input | Output |
|---|---|---|
| `detect_repo` | file_path | repo_id, profile_status (loaded/none/pack-available) |
| `get_archetype` | repo, file_path | archetype name, alternatives w/ confidence |
| `get_canonical_excerpt` | repo, archetype | annotated excerpt (500-800 tokens) |
| `get_rules` | repo, archetype? | rules + citations to canonicals |
| `lint_file` | repo, archetype, content | AST violations list |
| `get_drift_status` | repo | freshness + recommended action |
| `refresh_repo` | repo, force | re-analyze repo |
| `bootstrap_repo` | path, mode | first-time analysis + interview state |
| `list_profiles` | — | all known repos |
| `apply_profile_pack` | repo, pack | install pre-built profile |
| `refine_profile` | repo, feedback | apply user-driven correction |

State at:
- `${CLAUDE_PLUGIN_DATA}/profiles/<repo_id>/` (per-user cache, fallback)
- `<repo>/.chameleon/profile.json` (committed, team-shared, **default**)

Priority order on read: in-repo > per-user cache > profile pack match.

---

## Multi-language AST extractor layer

Each language plugin under `mcp/chameleon_mcp/extractors/<lang>.py` with this protocol:

```python
class Extractor(Protocol):
    def parse_repo(repo_path: Path, glob: str, limit: int = 200) -> ParseResult: ...
    def extract_archetypes(parse_result: ParseResult) -> list[Archetype]: ...
    def extract_patterns(parse_result: ParseResult) -> dict[str, Any]: ...
    def cluster_files(parse_result: ParseResult) -> dict[str, list[Path]]: ...
```

**v1 language support:**
- Ruby (Prism via subprocess) — proven approach inherited from claude-measure-twice
- TypeScript / JavaScript (TS Compiler API via subprocess)
- Python (libcst, ast stdlib)

**v1.5 stretch:**
- Go (go/parser subprocess)
- Rust (syn subprocess)
- PHP (nikic/PHP-Parser)
- Java (JavaParser)

Each extractor outputs to a normalized `ParseResult` schema so downstream dimension extraction is language-agnostic at the analyzer level.

---

## Profile schema

```
.chameleon/profile.json   (committed, team-shared default)
  OR
${CLAUDE_PLUGIN_DATA}/profiles/<repo_id>/   (per-user cache fallback)
  ├── manifest.json        # repo_id, last_analyzed, source (bootstrap|pack), version
  ├── archetypes.json      # path patterns → archetype name + globs + confidence
  ├── rules.json           # per-archetype rules + citations to canonical examples
  ├── canonicals.json      # chosen reference files + line ranges + sha
  ├── idioms.md            # human-curated free-form (escape hatch)
  └── drift.db             # sqlite mtime/sha map for drift detection
```

`archetypes.json` shape (illustrative):

```json
{
  "version": 1,
  "archetypes": {
    "api-controller": {
      "paths": ["app/controllers/api/v1/**/*.rb", "app/controllers/api/v2/**/*.rb"],
      "alternatives": ["admin-controller"],
      "canonical": {
        "path": "app/controllers/api/v1/users_controller.rb",
        "lines": [1, 80],
        "sha": "abc123..."
      },
      "confidence": 0.94,
      "source": "bootstrap"
    },
    "query-hook": {
      "paths": ["src/queries/**/*.ts", "src/hooks/use*Query.ts"],
      "alternatives": [],
      "canonical": {
        "path": "src/queries/useUserQuery.ts",
        "lines": [1, 60],
        "sha": "def456..."
      },
      "confidence": 0.88,
      "source": "bootstrap"
    }
  }
}
```

---

## Profile pack registry

Pre-built profiles ship as either:
- **Built-in packs** at `${CLAUDE_PLUGIN_ROOT}/packs/<pack-name>/` (bundled with plugin)
- **Companion plugins** that register additional packs at install time

**Match strategy** (priority order):
1. Git remote URL exact match (e.g., `git@bitbucket.org:empire-flippers/api.git` → load `empire-flippers-api` pack)
2. Signature heuristics (Gemfile contents, package.json deps, characteristic file paths)
3. Explicit user assignment via `/chameleon-apply-pack <name>`

**EF migration path:**
- Existing `claude-measure-twice` profiles → migrated as `packs/empire-flippers-api/` and `packs/empire-flippers-client/`
- Plugin auto-detects EF repos via remote URL signal and applies packs without bootstrap
- Zero loss of EF hand-curated work; promoted from "the whole plugin is EF-specific" to "EF is one of N supported pre-built profiles"

---

## Bootstrap interview flow (`/chameleon-init`)

```
1. User explicitly invokes /chameleon-init in a repo
   (No auto-trigger — confirmed user preference)

2. Engine:
   a. Detect language(s) via heuristics:
        Gemfile → Ruby, package.json → JS/TS, go.mod → Go,
        pyproject.toml/requirements.txt → Python, Cargo.toml → Rust, etc.
   b. Load relevant extractor(s)
   c. AST scan repo:
        - if repo file count < 500: full pass
        - else: stratified sample (top-N by directory frequency, recent commits)
   d. Statistical pattern extraction:
        naming conventions, file/folder structure, import order,
        type annotation density, error handling patterns, test placement
   e. Archetype proposal:
        "I detected: controllers, models, jobs, services. Sakto ba? Naa'y archetype nga di nako na detect?"
   f. User confirms / corrects archetypes
   g. Structural rule proposal:
        "Snake_case files, 2-space indent, single-quote strings, sorted imports. Confirm?"
   h. User confirms / corrects
   i. Idiom prompt (free-form):
        "Naa'y team-specific libraries, banned imports, custom hooks, error patterns ko nga dapat mahibal-an? Anything that wouldn't show up in normal AST analysis?"
   j. User responds with idioms
   k. Engine generates: archetypes.json + rules.json + canonicals.json + idioms.md
   l. Save destination prompt:
        "Save to .chameleon/profile.json (committed, team shares) or per-user cache (private)? [committed]"
   m. Reports: "Profile ready. N archetypes, M rules, K idioms. Cost: $X.XX. Drift tracking enabled."
```

**Cost estimate per bootstrap:**
- AST scan: 0 Claude tokens (local computation)
- Sampling reads: ~50-100 files × ~500 tokens each = ~25-50k tokens
- Interview turns: ~5-10 turns × ~2k tokens each = ~10-20k tokens
- Profile generation output: ~5-10k tokens
- **Total: $1.50–$5.00 per repo (one-time investment)**

---

## Multi-repo handling

- Profile keyed by `repo_id` (sha256 of git remote URL OR repo absolute path if no remote)
- Storage:
  - In-repo: `<repo>/.chameleon/profile.json` (preferred default — team shares the brain)
  - Per-user cache: `${CLAUDE_PLUGIN_DATA}/profiles/<repo_id>/` (fallback when in-repo not present)
- Detection: file-path walk-up on each tool call (NOT cwd) — handles editing across repos in same session
- Drift tracking: per-repo sqlite mtime/sha database

**Onboarding flow across multiple repos:**
- First time in repo A → user runs `/chameleon-init` → profile saved
- Switch to repo B (no profile) → SessionStart primer suggests `/chameleon-init`
- Back to repo A → SessionStart primer loads existing profile, no re-bootstrap
- Pre-built pack match (e.g., EF api/client) → auto-applied via remote URL signal

---

## Plugin coexistence

**Hygiene rules:**
- Slash commands namespaced: `/chameleon-*`
- Env vars namespaced: `CHAMELEON_*`
- Skills `paths:` glob — won't fire on irrelevant files
- Hooks compose with other plugins' hooks (Claude Code stacks them on shared matchers)
- Inject context, don't deny (security layer is the only hard-deny)
- Token budget discipline: ~500 prime + ~500-800 per-edit injection
- Distinct MCP server (`chameleon-mcp`), no namespace collision

**Specifically with superpowers:**
- Superpowers operates at "how to approach work" layer (process: brainstorm, debug, plan, review)
- chameleon operates at "how to write code that fits this repo" layer (output shape: file/naming/idiom conformance)
- Complementary, not overlapping
- Both can run together; agent gets both process discipline + pattern conformance

---

## Cost model

| Scenario | Estimate | Notes |
|---|---|---|
| SessionStart prime | ~500-800 tokens | using-chameleon SKILL.md + profile primer |
| Per-edit context injection | ~500-800 tokens | canonical excerpt + rules |
| Per-Write retroactive | ~500-800 tokens | compensation for Skills paths: gap |
| Archetype skill load | ~200-500 tokens | only on path match |
| **Steady-state per session** (Sonnet 4.6, 30 turns, warm cache) | **$0.30–0.40** | matches claude-measure-twice + superpowers ceiling |
| **Per-month at 100 sessions** | **$30–40** | comfortably under $50 ceiling |
| **Bootstrap per repo (one-time)** | **$1.50–5.00** | curated 50-100 file analysis + interview |
| **Per-team-month with 5 devs sharing committed profile** | **$150–200** | profile sharing eliminates redundant bootstrap |

(Token math validation requested in Round 1.)

---

## Phase plan (build sequence)

| Phase | Effort | Exit criteria |
|---|---|---|
| Phase 1 — Foundation | ~80h | All skills + hooks + MCP shell + plugin manifest tested green |
| Phase 2 — Extractors | ~60h | Ruby + TS + Python extractors working with test corpus |
| Phase 3 — Bootstrap engine | ~50h | `/chameleon-init` produces working profile on 5 test repos (varied stacks) |
| Phase 4 — Profile pack system | ~40h | EF packs migrated from claude-measure-twice; pack registry working |
| Phase 5 — Distribution | ~40h | Multi-harness sync scripts + marketplace publishing |
| Phase 6 — Conformance benchmarking | ~50h | 95%+ on archetype-matched tasks across 3 test repos |
| Phase 7 — Documentation + release | ~30h | Docs complete, dogfooding green for 2 weeks |
| **Total** | **~350h** | **~9 weeks of focused work** |

---

## Open decisions for Round 1 reviewers

1. ~~**Plugin name**~~ — **decided as `chameleon`** before review
2. **Profile location default** — committed `.chameleon/profile.json` (confirmed)
3. **Bootstrap consent** — explicit `/chameleon-init` only, no auto-trigger (confirmed)
4. **Initial language support** — Ruby + TS + Python in v1; Go/Rust/PHP/Java in v1.5
5. **Profile pack distribution** — built-in packs in `${CLAUDE_PLUGIN_ROOT}/packs/` + companion plugins for third-party
6. **Drift detection** — mtime/sha approach (cheap, local, no Claude tokens)
7. **MCP transport** — stdio FastMCP (proven approach)
8. **Skill cross-references** — `chameleon:skill-name` style, no `@` links (matches superpowers convention)
9. **Bootstrap cost ceiling** — accept $1.50–5/repo (one-time)
10. **Steady-state target** — <$0.40/session (matches superpowers ceiling)

---

## Areas explicitly seeking review (Round 1 reviewers)

**Cost analyst** — Validate token math, prompt cache utilization assumptions, latency budget. Identify hotspots where steady-state cost could blow through budget. Verify the 5-min cache TTL is workable for typical session patterns.

**Claude Code platform expert** — Verify hook events (SessionStart matchers, PreToolUse|PostToolUse|UserPromptSubmit), `mcp_tool` hook syntax (native in 2.1.138+), `paths:` Skills mechanism (known Write gap), marketplace mechanism, multi-harness compatibility. Flag any API assumptions that may have drifted.

**Pattern detection expert** — Viability of auto-archetype detection across language/framework variety. Where does AST + statistical analysis fall short? Which idioms are inferable vs require human curation? How robust is the path-cluster → archetype mapping for unconventional repo layouts?

**Security reviewer** — Profile storage security (committed `.chameleon/profile.json` — what if it leaks repo intel?), multi-repo data isolation, secret handling in committed profiles (canonicals.json could quote sensitive code), preflight-check safety preservation across new architecture, MCP server attack surface.

**DX reviewer** — First-time bootstrap UX (how confusing is the interview?), multi-repo handling (what if user works in 20 repos?), profile sharing/teamwork model (what if profile drifts in PR review?), error recovery (what if AST parse fails mid-bootstrap?), drift handling UX (when do users get prompted to refresh?).

---

## Out of scope for v1

- Cross-repo pattern transfer (e.g., "this team uses Pattern X across all their repos")
- Auto-PR opening for profile updates (manual `/chameleon-refresh` only)
- Full-history learning (only AST snapshot at bootstrap time; future versions could ingest git log)
- IDE-specific features beyond Claude Code harness compatibility
- Profile diffing UI (text-only diffs in v1)
- Cost telemetry dashboard (CLI only in v1)

---

## Inheritance from claude-measure-twice

What's preserved (the proven pieces):
- Preflight-check safety hard-deny layer (lines 1-556 of original — security-load-bearing)
- Posttool-recorder HMAC exec log (with GC bug fix)
- Callout-detector frustration phrase reminder
- Ruby Prism + TS Compiler API extractor approach
- MCP server + Skills paths: + PostToolUse Write compensation pattern
- Cost ceiling targets ($0.40/session, $50/month)

What's discarded (the not-yet-validated pieces):
- EF-specific archetype taxonomy (becomes a profile pack, not core)
- 8 EF-specific idioms hard-coded in plugin (move to pack idioms.md)
- Hand-curated canonicals committed in plugin repo (becomes per-repo)
- Bitbucket-specific drift-check pipeline (will design generic version)
- Phase 4 dogfooding-window-only assumptions

What's redesigned from scratch:
- Bootstrap interviewer (was implicit; now explicit `/chameleon-init`)
- Multi-repo cache structure (was per-EF-repo; now generic repo_id keyed)
- Profile pack abstraction (didn't exist; new for generic engine)
- Skill registration (was static for EF archetypes; now dynamic per active profile)

---

*End of v1 architecture draft. Ready for Round 1 multi-agent review (5 reviewers, parallel).*
