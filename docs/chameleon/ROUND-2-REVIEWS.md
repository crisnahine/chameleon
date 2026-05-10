# Round 2 Adversarial Review Reports — chameleon v2 Architecture

> 5 parallel adversarial agents. Each tried to break the design from a specific angle. Reviews collected 2026-05-10.
> Source architecture: `/Users/crisn/Documents/Projects/chameleon/ARCHITECTURE.md` (v2).

## Severity summary

| Reviewer | Severity |
|---|---|
| Cost adversary | SIGNIFICANT |
| Pattern adversary | SIGNIFICANT, leaning BLOCKING |
| **Plugin compatibility adversary** | **BLOCKING** |
| **Bootstrap edge case adversary** | **BLOCKING** |
| Maintainability adversary | SIGNIFICANT |

Two BLOCKING severities. Architecture is salvageable but **not ready for Phase 1 implementation as designed**.

---

## Reviewer 1 — Cost Adversary (SIGNIFICANT)

### Worst-case scenarios that blow through claimed range

**1. tRPC-heavy bootstrap.** Generated tRPC types in `src/server/routers/*.ts` with no `__generated__/` path, no header comment, no `linguist-generated` flag. 4,000-file repo where 3,200 are codegen → AST scans all of them → archetype clustering produces garbage (60-80% are tRPC procedure shapes). User runs `/chameleon-refresh` ($1.50) + `/chameleon-refine` repeatedly ($2-3) to fix. **Effective bootstrap: $4-7, not $0.50-2.**

**2. Aggressive globs bypass 50k guard.** User says "use `--paths '**/*.ts'`" on a 1.2M-file pnpm monorepo with 320k TS files. Architecture only enforces 50k as default ceiling, not on user-supplied globs. **Bootstrap: $8-15, latency 4-8 minutes.**

**3. 200-turn refactoring marathons.** Doc claims $0.30-0.50/30-turn-session. Cumulative input scales linearly: 200 turns × 6k context → 1.2M tokens × ($0.30/M cached + $3/M new) = $3.60-6 input. Output 200 × 800 × $15/M = $2.40. **Realistic: $6-12.**

**4. Multi-repo blow-up beyond 20.** Consultants/contractors touch 40-80 repos/week. 50 repos × 1,500 prime tokens = 75k tokens just for primers. **Session cost: $2-4, not $0.80-1.20.**

**5. Cold-start morning multi-plugin.** chameleon + superpowers + 1-2 custom plugins = 3,500-5,000 prime tokens uncached. Cache write 1.25× penalty never quoted in arch. 100 cold-starts/month × $0.015 surcharge = $1.50.

### Cache breakage edge cases
- `lstat` results with mtime/inode → if in cached prefix, cache misses every turn
- HMAC-signed exec log → hook output non-determinism breaks prefix
- Repo switch via file-path walk-up → 2 prime injections in 2 turns
- Idle reprime invalidates per-edit injection caching for rest of session (~1.4× post-reprime)

### Hidden cost stack-ups
- 1,500-token cap is per-edit, not per-turn. Sum of all 4 hooks (preflight-and-advise + posttool-recorder + callout-detector + session-start) → realistic per-turn ceiling 1,750-2,000 tokens
- Output dominance: 4-8k output × $15/M = $0.06-0.12 regardless of input. Architecture barely mentions this.
- Companion plugin pack discovery latency: 200-500ms × 100 sessions/month = 30+ seconds of dead time

### Critical breakage
**Line 785 wrong by construction**: "Multi-repo session (20 open): $0.80-1.20" misses the consultant tier (50-80 repos). At 50 repos, cache-thrash cost = **$2-4/session**, not $0.80-1.20. Plugin's USP is "one install, multi-repo" — this cost case is the most likely to be experienced.

### Recommendations
1. Drop "$0.30-0.50 typical" framing; honest range $0.30-0.50 single-repo, $1-4 multi-repo, $7+ first-bootstrap-tRPC
2. State 30-turn assumption explicitly in every cost claim
3. Cap globs on bootstrap (post-glob 50k enforcement)
4. Specify cache_control discipline for hook outputs
5. Total-hooks-per-turn cap, not per-hook (≤2,000 tokens)
6. Cold-start cost line in table
7. Output-dominance line in table
8. Pricing-volatility hedge (state assumed pricing baseline)
9. Bootstrap cost-amortization math derivation

---

## Reviewer 2 — Pattern Adversary (SIGNIFICANT, leaning BLOCKING)

### Severity assessment
The `content_signal` schema as drafted is a **two-cell toy** (`directive` / `absent_directives`) being asked to carry weight it cannot bear. Several of the most common modern TS frameworks fall outside what it can express.

### Detection failures

**1. tRPC routers — canonical doesn't exist as a "file".** Builder chains, no canonical pattern file. AST sees "method calls everywhere".

**2. zod schemas mixed with computation.** Half-declaration, half-runtime. AST gets garbage. `paths` won't separate.

**3. Three-way React import variants without directives.** `react` vs `react-dom/server` vs `react/jsx-runtime` — same paths, no directive, three archetypes by import root. Schema has no `imports_signal` field.

**4. Pydantic v1 vs v2 not tractable.** Reliable signal is class-body inner Config (v1) vs `model_config = ConfigDict(...)` (v2). `content_signal` cannot express class-body shapes without becoming AST DSL.

**5. Effect's `gen` and ts-pattern's `match()`** produce identical-looking call graphs to plain functional code.

**6. Half-migrated codebases — bimodal surfacing helps until 6+.** Pages+App+Express+Hono+Mocha+Vitest+lodash+es-toolkit. 6 bimodal prompts in PROMPT 2 = 6 forced choices. User says "I don't know, leave it" → no fallback state.

### Schema gaps `content_signal` cannot express
- **Imports**: `imports_from`, `imports_root`
- **Type-level discriminants**: `as const`, branded types, template literal types
- **Decorators**: `@Injectable()`, `@Module()`, `@Controller()` (NestJS)
- **Class-body shapes**: Pydantic v1 inner Config
- **JSX semantic patterns**: server-component boundaries, rules-of-hooks
- **Multi-canonical archetypes**: schema is `canonical: { ... }` singular; teams have multiple idiomatic ways
- **Circular signals**: archetype A imports from archetype B's canonical → loaders may stack-overflow

### Confidence model breakage
`high|medium|low` is a relabel, not a fix. Threshold function unspecified. 51/49 split → "high" lies routinely.

### Critical breakage
**Pattern-detection promise undeliverable** for any framework whose archetypes are defined by import roots, decorator sets, type-level constructs, or class-body shapes — which is most of modern TS. Either:
1. Constrain v1 scope to "Next.js App Router + classic React + plain Node" with explicit "NestJS/tRPC/Effect not supported in v1"
2. Expand `content_signal` into real AST matcher language (triples extractor complexity)
3. Accept that idioms.md absorbs everything `content_signal` can't, and rebrand as "pattern *prompts*" rather than "pattern *detection*"

Architecture currently promises (1)+(2) and delivers neither.

---

## Reviewer 3 — Plugin Compatibility Adversary (**BLOCKING**)

### Severity
The architecture survives obvious collisions (namespacing, MCP server names) but breaks in two non-obvious places: (1) `<CHAMELEON_IMPORTANT>` insufficient because `using-superpowers` already pre-empts everything, and (2) chameleon's design assumes hooks merge cleanly across plugins — they do not.

### Multi-plugin context wall scenarios

**Scenario A: chameleon + superpowers + 1k-token third plugin.** Realistic floor ~3,700 tokens primed before user types. With 30-turn session × ~10 edits → 15,000 tokens of advisory text on top.

**Scenario B: 4+ companion packs.** Each is a real plugin with its own SessionStart hook. Quadruple- or quintuple-priming.

### Hook firing order surprises
Combined `preflight-and-advise` is internally sequential but **externally parallel** with other plugins' PreToolUse hooks. Buggy `pre-commit-validator` 60s timeout stalls every Edit while chameleon finishes in 50ms — user blames chameleon. No "skip MCP if any other PreToolUse will deny" coordination.

### Naming collisions
- `/cham-init` short alias resolution unspecified
- MCP tool collision: `chameleon-mcp::detect_repo` vs `measure-twice-mcp::detect_repo` — model picks wrong one when both present
- Skill `using-X` saturation — superpowers' "process skills first" priority leaves chameleon's "constraint layer" unplaced

### Update / version-mismatch breakage
Companion-plugin v1↔v2 mismatch unhandled. `chameleon-ef-pack@1.0.0` ships v2 schema; engine v1.1 with v3 schema → loader doesn't coerce, post-install hook silently writes garbage to `${PLUGIN_DATA}/packs/`.

**No engine-version compatibility field in pack manifest.**

### SessionStart JSON merging — the actual failure
Superpowers' release notes are explicit: *"Claude Code reads BOTH additional_context and hookSpecificOutput without deduplication"*. Superpowers learned this the hard way. **Chameleon's architecture (line 297-298) repeats both formats without flagging this same hazard.** If chameleon emits both formats, prime appears **twice** on Claude Code, stacks with superpowers' version. Token cost doubles silently.

### Critical breakage
**The first-use trust prompt collides with superpowers' implicit consent model and creates an interactive deadlock.** Superpowers' `using-superpowers` mandates Skill invocation precede *all* responses. If chameleon's SessionStart injects `[y/N]` prompt as additionalContext, model deadlocks: superpowers says "invoke skill before any response," chameleon says "answer y/N before doing anything." Manifests as ignoring trust prompt entirely (silent trust) or invoking `brainstorming` to "think about whether to trust" — neither correct.

### Recommendations
1. **Drop `<CHAMELEON_IMPORTANT>` entirely.** Use neutral `<chameleon-context>`. Don't compete on framing.
2. **Add `engine_min_version` and `engine_max_version` to pack manifest schema.**
3. **Mirror superpowers' platform-dispatch logic exactly** — emit only one of three context fields per platform. Add regression test.
4. **Document priority order with superpowers** in `using-chameleon`: "After `using-superpowers` triggers `brainstorming`, but before any Edit/Write."
5. **Coordination signal** for other PreToolUse hooks (env var `CHAMELEON_ADVISORY_INFLIGHT=1`).
6. **Max companion packs guard** at engine load (e.g., 10).
7. **Acceptance test must run with superpowers also installed.**

---

## Reviewer 4 — Bootstrap Edge Case Adversary (**BLOCKING**)

### Severity
Five scenarios produce a profile that **looks fine** in summary, gets committed to `.chameleon/`, passes PR review, and **actively misleads Claude**:

**1. Archive-majority repo** — `legacy/` 4,000 vs `current/` 800. Stratified sampling picks legacy as majority. Canonical = jQuery-era pattern. New work steered backward. **No detection mechanism.**

**2. Test-heavy repo** — 90% of TS in `__tests__/`. Canonical = test file shape. New feature work shaped like tests.

**3. pnpm/yarn workspace root bootstrap** — collapses 3 distinct workspaces into one profile. Cross-workspace conventions homogenized.

**4. Plugin-loaded `.prettierrc`** — `{"plugins": ["prettier-plugin-tailwindcss"]}` — bootstrap can't load JS, plugin-specific rules invisible. Profile claims "rules from .prettierrc" but silently drops half.

**5. Half-migrated parallel maintenance** — bimodal forced to binary; whichever picked, other subtree drifts forever, `chameleon-refresh` keeps flagging it.

### Repo structure breakage
- Vendored monorepos kill 50k file-count guard (vendor/ has 80k checked-in files; pre vs post-exclusion ambiguous)
- Submodule repos: `repo_id` from outer remote, but inner submodule has different conventions
- Recursive: chameleon-ef-pack repo bootstrapping itself (graceful error UX undefined)
- Empty/test-only repos: bootstrap clusters zero/test files

### Pathological inputs
- Case-insensitive macOS + case-sensitive git: `MyFile.ts` and `myfile.ts` collide
- BOM/CRLF: line ranges off by N
- Unicode/emoji NFD vs NFC normalization: different `repo_id` cross-platform
- Hardlinks bypass symlink defense (`os.lstat` only refuses `S_ISLNK`)

### Race conditions
- Ctrl-C between AST scan and save → partial profile loads
- Concurrent `/chameleon-init` on shared box: last-writer-wins, no lock file
- Disk full during sqlite write → drift.db corrupts; behavior unspecified

### Tool config edge cases
- Prettier-vs-ESLint conflict (singleQuote vs double): silent pick by file iteration order
- Multiple `.prettierrc` per workspace
- `.editorconfig root: false` inheritance walk
- Plugin-loaded prettierrc rules invisible
- `tsconfig.json extends` chain unhandled

### Bimodal/sparse UX breakage
- 6+ bimodals violate "≤3 user-facing prompts" cap
- 8 sparse clusters: ask 8 times or auto-merge into junk-drawer "misc"
- Bimodal where minority is canonical for new code (recent migration) — anchoring bias

### Recommendations
1. **Per-workspace bootstrap detection** (pnpm-workspace.yaml, lerna.json, package.json#workspaces, nx.json)
2. **Recency-weighted clustering** (last 90 days = 2x vote)
3. **Atomic write protocol** (.chameleon/.tmp/ + atomic rename, .lock file)
4. **Hardlink defense** (inode dedup)
5. **Canonical filters**: exclude `__tests__/`, `legacy/`, `archive/`, `deprecated/`
6. **Bimodal threshold trigger >3** flips to batch UX
7. **Per-workspace tool config scoping**
8. **Surface excluded files** (.chameleon/.skipped.log)
9. **Schema-version compat check on load**
10. **Refuse zero-archetype profiles**

---

## Reviewer 5 — Maintainability Adversary (SIGNIFICANT)

### Critical breakage (irreversible long-term cost)
1. **Profile schema content shape is committed-to-git.** Once teams have `.chameleon/profile.json` files, schema changes cause merge conflicts. Speculative future fields become permanent migration debt.
2. **`chameleon-ef-pack` separation correct but binds engine to vendor-publisher model forever.** Sigstore key infrastructure, pack manifest format become permanent ABI.
3. **Real Problem Evidence section gates release.** If dogfooding fails, architecture forces honest renegotiation OR shipping with a lie. Healthy if owned, ruinous if drifted past. Recommendation: CI check on release tag (no semver-stable without transcripts).

### Platform API drift (12-24 months)
- `mcp_tool` PreToolUse hook deprecation (no abstraction layer, full dispatch path collapses)
- Hook ordering controls land → combined preflight-and-advise becomes legacy over-engineering
- 5th canonical skill root added → MCP-driven dispatch becomes second-best
- `additionalContext` schema fragmentation across 4+ harnesses

### Dependency drift (rots first)
1. **TS Compiler API** (6-12 months) — pins nothing; minor versions every 2 months rename APIs. Vendor TypeScript at known version inside plugin.
2. **FastMCP** (6 months) — sub-1.0, breaking changes likely
3. **detect-secrets/gitleaks rule files** — ship pinned (false positive creep) or fetch live (network dep) — unaddressed
4. **Python 3.11 minimum** — EOL Oct 2027
5. **Node version for ts_dump.mjs** — unspecified, LTS rotates yearly
6. **SQLite library churn** — WAL mode, busy_timeout defaults shift between Python versions

**No `requirements.lock` / `package-lock.json` story.** Single biggest miss.

### Schema versioning breakage
- No migration script path
- No "engine supports schemas vN..vN-2" backward window policy
- No fail-closed for unknown future versions
- No companion-plugin pack schema versioning

### Profile rot 12-24 months
- `canonicals.json` cites `path + lines + sha`. Within 6 months, ~30% of canonicals will have stale shas. MCP `get_canonical_excerpt` behavior on sha mismatch undefined.
- Archetype clusters drift; no incentive structure for users to run `/chameleon-refresh` (no staleness notification)
- `idioms.md` grows monotonically; no deprecation tracking
- Schema migration of committed profiles is worst case

### Companion plugin ecosystem
- Signing key ownership: solo maintainer = single key holder = full bus-factor risk; no rotation
- Pack discovery at 50+: no registry, no search
- Pack abandonment: no "stale pack age-out" warning
- Spoofable git-remote match: namespace pack IDs as `<publisher>/<pack>`
- `--allow-unsigned` flag: gets globally aliased (`CHAMELEON_ALLOW_UNSIGNED=1` in shell rc)

### Documentation/test drift
- No issue tracker tie-in for "open decisions for Round 2"
- Phase plan effort estimates explicitly fabricated
- No `MAINTAINER.md` listing key rotation, dep upgrade cadence, schema migration runbook
- Acceptance test repo: untracked sub-project

### Recommendations
1. Add "Versioning & Compatibility" section
2. Canonical references via AST queries (not strict sha matching)
3. Bake staleness into primer
4. Companion-pack registry concept
5. Schema migration directory established in v1
6. Decision register doc (`docs/chameleon/decisions/`)
7. Lock files in repo
8. Pack `--allow-unsigned` as session-only, never persisted
