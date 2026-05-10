# Round 1 Review Reports — chameleon v1 Architecture

> 6 parallel reviewer agents, each with a specific lens. Reviews collected 2026-05-10.
> Source architecture: `/Users/crisn/Documents/Projects/chameleon/ARCHITECTURE.md` (v1).

## Approval level summary

| Reviewer | Approval level |
|---|---|
| Cost analyst | APPROVED WITH CONCERNS |
| Claude Code platform expert | APPROVED WITH CONCERNS |
| Pattern detection expert | APPROVED WITH CONCERNS |
| Security reviewer | APPROVED WITH CONCERNS |
| DX reviewer | APPROVED WITH CONCERNS |
| Jesse Vincent perspective (emulated) | NEEDS MAJOR REVISION (leaning WOULD REJECT AS SLOP) |

5 of 6 approved-with-concerns, 1 flagged for major revision. The architecture is fundamentally sound but has multiple concrete issues that must be resolved before implementation.

---

## Reviewer 1 — Cost Analyst

**Approval:** APPROVED WITH CONCERNS

### Token math validation
- **SessionStart prime undersold**: claim 500-800 tokens, but `using-superpowers/SKILL.md` alone is ~1,050 tokens. Realistic budget: 1,200-1,800 tokens including profile primer.
- **Per-edit injection**: 500-800 tokens claim verified for the excerpt+rules portion, but hook stack (preflight + archetype-advisor + posttool-write-fix on Writes) can stack ~1,500-2,400 tokens per Write event.
- **Steady-state $0.30-0.40**: math holds for single-repo, continuous-work sessions. ~$0.34 by rough calculation, but fragile.
- **Bootstrap $1.50-5.00**: doesn't add up to my arithmetic ($0.30 by my math). Either undocumented file-read sizes or longer interview than stated.
- **Per-team-month $150-200**: arithmetic unsourced ($175 = 5×$35 fits but range floor $150 implies extra savings not explained).

### Cost hotspots
1. **Multi-repo per session is unbudgeted.** 20 open repos = 20× prime cost + cache thrashing.
2. **Hook stack on heavy edit sessions.** 30 edits × 1,200 tokens injected = 36k tokens, 10× swing between cache-warm and cache-cold.
3. **No explicit cache breakpoint strategy.** If injections sit mid-conversation, prefix breaks per turn.
4. **Output tokens dominate at $15/M.** Verbose responses break budget faster than long contexts.

### Prompt cache analysis
5-min TTL workable for typical sessions, but architecture silent on:
- Where cache breakpoints sit (no `cache_control` mentioned)
- Idle gap behavior (>5 min triggers full reprime)
- Multi-repo session repos thrash cache

### Recommendations
1. Add explicit cache strategy section (one paragraph)
2. Revise prime budget to 1,200-1,800 tokens (not 500-800)
3. Add "Multi-repo cost scaling" subsection
4. Document bootstrap math derivation
5. Source per-team-month derivation
6. Cap hook-stacked injections per turn (suggest 1,500 max)
7. Acknowledge output cost dominance

### Critical issues
None blocking, but four hotspots need to be costed before claiming hard $0.40 ceiling. Honest range would be "$0.30-0.50 in most sessions, with multi-repo and heavy-edit higher."

---

## Reviewer 2 — Claude Code Platform Expert

**Approval:** APPROVED WITH CONCERNS

### Platform API validation
| Claim | Verdict |
|---|---|
| SessionStart matchers `startup\|clear\|compact` | CORRECT but incomplete (docs list 4: also `resume`, omitted intentionally per superpowers) |
| `type: "mcp_tool"` native | CORRECT, but **"2.1.138+" version stamp is unsourced — drop it** |
| SessionStart JSON dispatch (3 formats) | CORRECT, exact mirror of superpowers |
| `${CLAUDE_PLUGIN_ROOT}` / `${CLAUDE_PLUGIN_DATA}` | CORRECT |
| Plugin manifest format | CORRECT |
| Marketplace auto-update | CORRECT (version pinning in `plugin.json` controls rollout) |
| `disable-model-invocation: true` | CORRECT |
| Polyglot wrapper `run-hook.cmd` | CORRECT |
| Bootstrap pattern (SessionStart inject SKILL.md) | CORRECT |
| Hook stacking on shared matchers | **MOSTLY CORRECT BUT SURPRISE: hooks run in PARALLEL, not sequential** |
| Skills `paths:` mechanism + Write gap | **PARTIALLY CONFIRMED — Write-gap claim is folklore, validate empirically** |

### Drift risks
1. Hook parallelism contract — preflight-before-advisor sequence not enforceable
2. `paths:` semantics on Write — undocumented platform behavior
3. `mcp_tool` hook stability — moving target without version stamp
4. Plugin data cleanup — orphaned versions cleaned 7 days after update

### CRITICAL: Dynamic skill registration won't work
The architecture proposes runtime-generated skill files at `${PLUGIN_DATA}/runtime-skills/<repo_id>/...`. **Claude Code only scans 4 canonical roots** (enterprise, personal, project, plugin) — not arbitrary plugin data dirs. The dynamic archetype skills design **will not be discovered**.

Three alternatives:
- (A) Project-scoped skills: write to `<repo>/.claude/skills/` (pollutes user's repo)
- (B) **MCP-driven dispatch**: `using-chameleon` instructs agent to call MCP tools per edit; PreToolUse `mcp_tool` hook already does this
- (C) Pre-generate at install/refresh into `<plugin>/skills/` (requires session restart)

**Recommendation: option B.** Drop dynamic skill registration entirely.

### Recommendations
1. Drop "2.1.138+" version stamp
2. Restructure PreToolUse stack to acknowledge parallelism
3. Replace dynamic archetype skills with MCP-driven dispatch
4. Add `resume` matcher consideration with rationale
5. Validate `paths:` Write-gap empirically before relying on `posttool-write-fix`
6. Pin plugin `version` explicitly

### Critical issues
1. **Dynamic skill registration as designed will not be discovered by Claude Code.** Must redesign before Phase 1.
2. **Hook ordering assumption (preflight before advisor) is not platform-enforceable.** Today they run in parallel.

---

## Reviewer 3 — Pattern Detection Expert

**Approval:** APPROVED WITH CONCERNS

### Auto-derivable dimensions

**Reliably auto-derivable (high confidence):**
- File/folder layout, naming conventions
- Indentation, quote style (defer to Prettier/RuboCop config files first)
- Import ordering and grouping
- Type annotation density
- Test file colocation pattern
- Public API surface shape

**Auto-derivable but noisy:**
- Archetype names (clustering finds shape, not concept)
- Error-handling patterns
- Canonical excerpt selection (longest ≠ best)

**Not auto-derivable (idioms.md territory):**
- Why patterns exist
- Banned imports / deprecated paths
- Domain vocabulary
- Implicit invariants
- Cross-cutting conventions
- Migration state
- Team taste

### Detection failure modes
1. Rails monolith with engines (path clustering fragments archetypes)
2. **Next.js 14 App Router + RSC + Server Actions** — distinguished by file content (directives), not paths
3. FastAPI + Pydantic v1+v2 coexistence (per-file version detection needed)
4. Django mixed CBV/FBV (per-symbol, not per-file archetypes)
5. Monorepo with multi-stack (need per-subtree scoping)
6. DSL-heavy Ruby (Prism parses syntax, not semantics)
7. JSX/TSX semantic patterns (rules-of-hooks beyond AST)
8. **Metaprogramming blind spot** (method_missing, __getattr__, decorators)
9. **Generated code not excluded** — phantom archetypes
10. **Half-migrated codebases** — bimodal distributions silently picked
11. Sparse archetypes (<5 files) need special handling

### Idiom inferability
**Inferable**: pagination patterns, soft-delete, service object call style
**Not inferable**: prohibitions ("never use .find"), mandatory wrappers, migration state, error-handling helpers

The interview prompt is too vague. Replace with category checklist (banned imports, mandatory wrappers, error helpers, logging keys, feature flag style, deprecated paths).

### Confidence scoring
**`confidence: 0.94` is currently not meaningful.** Recommend:
- Define formula explicitly OR
- Use ordinal bands (high/medium/low) with thresholds

Current schema lacks `cluster_size` and `outlier_paths` fields — both essential.

### Recommendations
1. **Read tool config files first** (.rubocop.yml, .prettierrc, etc. as ground truth)
2. **Generated-code detection in Phase 2** (Phase 6 too late)
3. **Content-based archetype variants** — add `content_signal` field
4. **Per-symbol archetypes** for mixed-pattern files
5. **Bimodal distribution surfacing** in interview
6. **Category checklist** for idioms prompt
7. **Sparse cluster handling** (`min_cluster_size: 5`)
8. **Confidence as ordinal bands**, not floats
9. **Engine/subtree awareness** for monorepos
10. **Document metaprogramming blind spot** in `using-chameleon`

### Critical issues
- **Generated code not excluded from bootstrap clustering** — phantom archetypes
- **`paths`-only archetype matching breaks on modern frameworks** (Next.js, mixed-CBV/FBV) — need `content_signal`

---

## Reviewer 4 — Security Reviewer

**Approval:** APPROVED WITH CONCERNS

### Confidentiality risks
1. **`canonicals.json` may leak secrets** — bootstrap picks files by frequency, not security review. Hardcoded test API keys, internal hostnames, PII routinely appear. Once committed, never recoverable.
2. **`idioms.md` user-typed often pasted with real values**
3. Internal hostnames, private repo URLs, S3 buckets in code comments
4. **`drift.db` is a directory enumeration** — useful for attackers if committed
5. HMAC key per-user not per-session — leaked home access = forge log entries

### Integrity risks
1. **Profile pack matching by git remote URL is spoofable** — typo-squat, fork takeover
2. **Companion plugin profile packs may ship hooks/scripts** — execute arbitrary code
3. **Dynamic archetype skills** are generated from profile data → prompt injection vector
4. **Inherited preflight bug — exec_log path mismatch.** posttool-recorder writes to `${TMPDIR}/.claude_exec_log/` (per-user macOS), preflight reads from hardcoded `/tmp/.claude_exec_log/`. **HMAC verification silently fails — bash_exit_ok stays true forever.**
5. **Preflight is 1001 lines, not 556** — architecture's "verbatim 1-556" claim is wrong (doc stale or already trimmed)
6. **Archetype-advisor MCP TOCTOU** — symlink swap can exfiltrate `~/.aws/credentials` into model context
7. **Path traversal via repo_id** if not always sha256

### Availability risks
1. Bootstrap on 1M-file monorepo (filesystem walk itself is DoS)
2. AST extractor subprocess fork bombs (regex-DoS, parser blowup)
3. **`drift.db` unbounded growth** — multi-GB across 30+ repos over a year
4. Preflight ERR trap fail-open silently disables safety on bug
5. Profile pack registry signature heuristics across N×M

### Threat model gaps
1. **Untrusted repo opening** — committed `.chameleon/profile.json` is attacker-controlled data
2. **No signature/integrity check on committed profiles**
3. macOS multi-user / shared dev box / containerized CI
4. MCP stdio transport assumes trusted parent
5. HMAC key generation silent failure mode
6. **Bootstrap interview output sanitization** missing
7. **`.gitignore` enforcement** — `drift.db` and runtime caches must be force-ignored

### Recommendations
1. Profile pack signing (Sigstore/cosign or ed25519)
2. **Canonical excerpt secret scanner** before write (detect-secrets/gitleaks)
3. Profile-pack loaders are data-only (forbid hooks/scripts in pack dirs)
4. **Fix exec_log path mismatch in port** (use `${TMPDIR:-/tmp}` consistently)
5. **drift.db is local-only by contract** — auto-`.gitignore`
6. **Symlink check in MCP `get_canonical_excerpt`** (lstat)
7. **Schema-validate profile.json on load** (strict JSON schema)
8. Rate-limit / size-cap idioms.md (50 KB hard limit)
9. Repo size guard at bootstrap (50k file ceiling)
10. AST extractor subprocess limits (memory + CPU + file size)
11. First-use prompt for committed profiles (trust-on-explicit-consent)
12. HMAC key fail-loud on errors

### Critical issues
1. **"Verbatim inheritance" claim is wrong** (preflight is 1001 lines, doc says 556)
2. **Inherited HMAC bypass via /tmp vs $TMPDIR mismatch** — must fix during port
3. **No profile-pack code-execution ban** — must be explicit
4. **Canonical excerpts unscanned for secrets** — single highest confidentiality risk
5. **Symlink TOCTOU in MCP file reads** — exfiltration vector

---

## Reviewer 5 — DX Reviewer

**Approval:** APPROVED WITH CONCERNS

### First-time experience walkthrough (Sam, senior eng, 30 min)
- 0-2 min: Install — fine
- 2-4 min: SessionStart primer suggests `/chameleon-init` but doesn't define "profile" — **first friction**
- 4-5 min: `/chameleon-init` fast detection, 0 tokens
- 5-15 min: **Interview is 6-10 turns** (too many)
- 15-17 min: **Free-form idiom prompt** — first-timer types "skip" because doesn't know what to put — **second friction**
- 17-18 min: Save location → committed → `git status` shows multiple files including `drift.db` — **third friction**
- 18-25 min: Edit works, payoff moment ✓
- 25-30 min: Pre-commit hits binary `drift.db` — confusion

### Friction points
1. Interview length (6-10 turns vs ESLint init's 5)
2. **`drift.db` committed** = binary churning + merge conflict factory
3. **`canonicals.json` referencing line ranges** — invalidates when files edited
4. **PR review of profile changes** — no human-readable summary
5. **Merge conflicts on profile.json** across parallel branches
6. SessionStart primer fires every session in scratch/docs repos — needs opt-out
7. **`<EXTREMELY_IMPORTANT>` collision with superpowers** — both inject same wrapper
8. JSON + freeform markdown mix in same dir

### Discoverability gaps
1. **8 slash commands too many** — realistic recall is 3 (`init`, `refresh`, `status`)
2. **`/chameleon-` prefix is 11 chars** — typing pain. Recommend **`/cham-` alias**
3. No archetype list in SessionStart primer
4. `using-chameleon` skill name reads weirdly

### Multi-repo UX (Maya, freelancer, 15 client repos)
- 5 TS, 4 Ruby, 3 Python, 2 PHP (unsupported), 1 Go (unsupported)
- **Bootstrap cost: $9-30 in first morning** — feels like surprise tax, not "one-time"
- **PHP/Go repos: error UX undefined** — kill plugin within a day
- **No EF pack helps her** — always cold-start path
- `/chameleon-status` ambiguous which repo
- Disk usage: 15 caches + 15 committed profiles + 15 drift.dbs

### Recommendations
1. **Cut interview to 3 prompts max** (archetypes only)
2. **Move `drift.db` out of `.chameleon/`** to per-user cache only
3. **Generate `profile.summary.md`** for PR review
4. **Add `/cham-` short alias**
5. **Consolidate 8 commands to 5** (merge doctor+status, fold apply-pack into init flag)
6. **Per-repo opt-out signal** (`.chameleon/.skip` or `CHAMELEON_DISABLE`)
7. First-session welcome message with cost estimate
8. Surface cost in primer ("Last session: $X")
9. **Error recovery section** in doc (parse fail, partial bootstrap, Ctrl-C resume)
10. Coordinate `<EXTREMELY_IMPORTANT>` with superpowers
11. Document combined-plugin token budget

### Critical issues
1. **`drift.db` committed to git** — churning binary diffs, merge conflicts
2. **Profile-as-PR-artifact unaddressed** — no human-readable summary
3. **No graceful degradation for unsupported languages** — plugin visibly broken in non-Ruby/TS/Python repos

---

## Reviewer 6 — Jesse Vincent perspective (emulated)

**Approval:** NEEDS MAJOR REVISION (leaning WOULD REJECT AS SLOP)

### Slop detection report
- Line 38 chameleon name justification — post-hoc marketing copy
- "Alternatives considered" naming table — theater, not real trade-offs
- **Tagalog/Cebuano interview prompts** — personal voice leaked, no localization story
- Cost table presents two-decimal precision without derivation
- Phase plan effort estimates ("80h") are made up

### Skills as code (BIGGEST GAP)
- 8 skills + dynamic family proposed
- **Zero test plans, zero baseline scenarios, zero rationalizations identified**
- `tests/skill-triggering/` is one line in file tree, not a plan
- **Iron Law violated**: "NO SKILL WITHOUT A FAILING TEST FIRST"
- `using-chameleon` Red Flags placeholder, not earned through pressure testing
- Dynamic per-archetype skills can't be eval'd (don't exist until runtime)

### Dependency discipline
- 6 runtime deps in v1: Prism, TS Compiler, libcst, FastMCP, SQLite, native parser shims
- Climbs to 10+ in v1.5
- **Self-contradicts** "Superpowers-style discipline" claim
- Honest framing: chameleon is a different beast (heavier by necessity). Either own it or drop the discipline claim.

### Bootstrap mechanism
- Lines 227-260 mirror `using-superpowers` pattern faithfully ✓
- **Acceptance test missing** — superpowers requires "Let's make a react todo list" → brainstorming auto-triggers. Chameleon equivalent should be "open profiled repo, send 'add a new endpoint', verify `using-chameleon` mandates `detect_repo` + `get_canonical_excerpt` before code"
- Non-profiled repo first-contact UX undefined

### Multi-harness reality check
- v1.5 directories listed (`.codex-plugin/`, `.cursor-plugin/`, `.opencode/`, `gemini-extension.json`, `GEMINI.md`)
- **Empty directories are theater** unless v1 ships at least one non-Claude harness
- Drop or commit to one extra harness

### Naming/documentation
- Skill names: mostly good ✓
- **`using-chameleon` description violates CSO rule** — summarizes workflow ("establishes pattern-conformance discipline and required MCP tool invocations") instead of triggering conditions only
- **"measure profile" leftover from rename** — copy-paste artifact
- Dynamic archetype description has same disease

### Critical issues
1. **No skill test plan** (Iron Law violation)
2. **Description fields summarize workflow** (CSO rule violation in shown examples)
3. **Dependency claim self-contradicts** (superpowers-discipline + 6 parsers)
4. **Bootstrap acceptance test undefined**
5. **Real-problem evidence thin** — no transcripts, no failure modes, no lived experience
6. **Profile-pack EF migration bundled with engine** — should be separate plugin per superpowers project-specific bloat rule
7. **Tagalog/Cebuano interview text** committed without localization plan

### What I would say if this were a PR
> Eight skills, no failing tests. The `using-chameleon` description summarizes workflow which is exactly the trap documented in `writing-skills`. Do the RED phase. Six bundled parsers is not "superpowers-style discipline." Bundle the EF migration as a separate plugin. The architecture asserts a problem without showing it. Strip v1.5 harness directories from v1 unless one ships. Acceptance test is missing. Come back when (1) is done. The rest is mechanical.
