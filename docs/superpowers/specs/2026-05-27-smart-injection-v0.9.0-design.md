# Smart Injection v0.9.0 - Design Spec

Auto-derive codebase conventions at bootstrap time. Inject them before every edit. Enforce them after every edit. Zero manual teaching required.

## Problem

Code reviewers repeatedly flag convention violations that chameleon doesn't catch: wrong imports (useQuery instead of useCustomQuery), wrong naming (missing I-prefix on interfaces), wrong patterns (render json: instead of render_data), reinventing existing utilities. Analysis of 1 month of PR reviews across empire-flippers/client and empire-flippers/api found 31 convention-related comments across 24 distinct patterns. Current chameleon catches structural violations (AST shape) but misses team conventions.

## Evidence Base

8 real codebases analyzed (bulletproof-react, ef-api, ef-client, excalidraw, forem, mastodon, maybe, plane). Each has 10-20 strong auto-detectable conventions. Two architecture review rounds and two expert reviews (plugin architect + LLM prompt engineer) validated and refined the approach.

## Architecture

### New artifact: conventions.json

Lives in `.chameleon/` alongside archetypes.json and canonicals.json. Produced at bootstrap time. Included in trust hash. All convention values pass through `sanitize_for_chameleon_context()`.

Schema:

```json
{
  "schema_version": 1,
  "generation": 1779801823,
  "min_sample_size": 10,
  "conventions": {
    "imports": {
      "<archetype_name>": {
        "preferred": [
          {"module": "useCustomQuery", "source": "@/hooks/useCustomQuery", "frequency": 47, "total": 52}
        ],
        "competing": [
          {"preferred": "useCustomQuery", "over": "useQuery", "preferred_count": 47, "over_count": 0}
        ]
      }
    },
    "naming": {
      "<archetype_name>": {
        "interface_prefix": {"pattern": "I", "consistency": 0.999, "sample_size": 2158},
        "type_prefix": {"pattern": "T", "consistency": 0.93, "sample_size": 42},
        "enum_prefix": {"pattern": "E", "consistency": 0.62, "sample_size": 8},
        "component_case": "PascalCase",
        "hook_prefix": "use"
      }
    },
    "inheritance": {
      "<archetype_name>": {
        "dominant_base": "ActiveInteraction::Base",
        "frequency": 0.82,
        "sample_size": 1414
      }
    },
    "method_calls": {
      "<archetype_name>": {
        "preferred": [
          {"method": "render_data", "over": ["render json:"], "frequency": 305, "over_count": 3}
        ],
        "common_top5": ["render_data", "before_action", "authorize_request", "render_error", "outcome.valid?"]
      }
    },
    "key_exports": {
      "<archetype_name>": ["useDebounce", "useCustomQuery", "useToggle", "formatCurrency", "slugify"]
    }
  }
}
```

Key schema changes from initial draft (per expert review):
- **naming is per-archetype** (not global) - monorepo workspaces can have different naming conventions
- **imports.competing runs on raw counts before frequency filtering** - prevents the two-sided filter from killing low-frequency-but-universal wrapper patterns like useCustomQuery (8% of all files but 100% of files that need query hooks)

### Phased delivery

**v0.9.0 MVP (3 days):** import frequency + naming patterns + SessionStart injection + PostToolUse lint for imports and naming.

**v0.9.1 (4 days):** inheritance analyzer + method-call frequency (requires ts_dump.mjs/prism_dump.rb extension) + PostToolUse lint for inheritance and method calls.

**v0.9.2 (3 days):** key_exports list + directory listing + convention-aware witness selection.

### Extractor 1: Import frequency analyzer (v0.9.0 MVP)

Input: `import_specifiers` from ParsedFile (already extracted by ts_dump.mjs / prism_dump.rb).

Algorithm per archetype cluster:

1. Count each import module across all files in the cluster (raw counts)
2. **Competing import detection first** (before any filtering): for each import module, check if its source file imports another module with a matching suffix/name. If `useCustomQuery`'s source file imports `useQuery`, they're a wrapper pair. Flag as "preferred useCustomQuery over useQuery" regardless of frequency percentage.
3. Then filter for the "preferred" standalone list:
   - Exclude framework-mandatory imports (>80% within THIS archetype, not cross-archetype)
   - Exclude rare imports (<5 occurrences, not percentage-based)
   - Surface remaining imports with count >= 10 as "preferred"
4. Skip if archetype has fewer than `min_sample_size` (10) files.

Competing import detection: check if the preferred module's source file re-exports or wraps the competing module (detectable at bootstrap by reading the wrapper file's imports). Fallback: substring matching (useCustomQuery contains useQuery) with minimum count threshold (preferred >= 5, competitor <= 2).

### Extractor 2: Naming pattern analyzer (v0.9.0 MVP)

Input: AST declarations from ParsedFile (interface names, type names, enum names, class names).

Per archetype:

TypeScript:
- Count I-prefixed vs non-prefixed interface declarations. If >80%, convention = "I-prefix for interfaces."
- Same for type (T-prefix) and enum (E-prefix).
- Scan component file exports for PascalCase. If >90%, convention.

Ruby:
- Module nesting style (compact `Foo::Bar` vs nested). Count per archetype.
- frozen_string_literal presence (first line scan via content_first_200_bytes).

Skip if fewer than 5 declarations of a given type exist in the archetype.

**Framing thresholds** (per Expert 2):
- Consistency >= 95%: inject as **enforced rule** ("Always I-prefix interfaces")
- Consistency 60-95%: inject as **strong convention** ("I-prefix interfaces (93%)")
- Consistency < 60%: **do not inject** (too noisy)

### Extractor 3: Inheritance analyzer (v0.9.1)

Input: `top_level_node_kinds` from ParsedFile (already contains `ClassNode:ApplicationRecord`, etc.).

Per archetype:
1. Count each `ClassNode:<superclass>` occurrence
2. If one superclass appears in >60%, it's the "dominant base"
3. For Ruby: also count `include` mixins

### Extractor 4: Method-call frequency analyzer (v0.9.1)

Input: requires extending ts_dump.mjs and prism_dump.rb to emit method-call identifiers.

**TypeScript CallExpression shapes captured:**
- Simple calls: `foo()` - emit `foo`
- Dotted calls: `this.foo()` - emit `foo`; `bar.baz()` - emit `bar.baz`
- Excluded: `new Foo()` (constructor), `Object.keys()` (stdlib), `console.*` (debugging)

**Ruby CallNode shapes captured:**
- Class-body-level method calls (not nested in method defs): `render_data`, `before_action`, `validates`
- This extends the existing DSL detection to also capture non-DSL method calls

Per archetype:
1. Count each method-call identifier across all files
2. Surface top-5 as "common methods"
3. Detect competing calls (same as import competing detection)

Implementation estimate: 3-4 days (touches ts_dump.mjs, prism_dump.rb, ParsedFile dataclass, extractor protocol).

### Key exports list (v0.9.2)

Extend ts_dump.mjs / prism_dump.rb to emit export NAMES (not just count). Per archetype: top-5 most-imported exports, prioritized by import count across the repo. Stored in conventions.json. Injected in SessionStart as "check before creating."

### Directory listing (v0.9.2)

At PreToolUse time, list 10-15 sibling files. Framed as instruction:

```
Nearby: useDebounce.ts, useCustomQuery.ts, useToggle.ts — check these before creating a new hook.
```

Only injected for Write (new file) or when the archetype has the key_exports convention. Omitted when directory doesn't exist or has 0 siblings.

### Convention-aware witness selection (v0.9.2)

When selecting canonical witnesses, prefer files that follow >95% conventions. If the best structural witness violates a strong convention (e.g., missing I-prefix), pick the next-best witness that follows it. Prevents the ~30% confusion rate when witness contradicts convention.

## Injection Design

### SessionStart (cached prefix)

Imperative framing for >95% conventions, context framing for 60-95%. Only conventions consistent across ALL archetypes go in SessionStart. Archetype-specific conventions go in PreToolUse.

```
<chameleon-conventions>
Follow these on every edit. Derived from this codebase (N files analyzed).

IMPORTS (enforce):
- Use useCustomQuery from @/hooks/useCustomQuery, not useQuery (100%)
- Tree-shaken lodash: import fn from 'lodash/fn' (98%)

NAMING (enforce):
- Prefix interfaces with I (IUserProps, IChartData) — 99.9%
- Prefix type aliases with T (TTheme, TRoute) — 93%
- PascalCase components, use-prefix hooks

REUSE (check before creating):
- Hooks: useDebounce, useCustomQuery, useToggle, useLocalStorage, useConfig
- Utils: formatCurrency, formatDate, slugify, buildQueryString
</chameleon-conventions>
```

~200-400 tokens. Re-injected after /compact (hook matcher: startup|clear|compact).

### PreToolUse Tier 2 (first edit per archetype)

Existing: archetype header + canonical witness (~500-1500 tokens).

Add (v0.9.2): directory listing with actionable framing (~30 tokens):

```
Nearby: ChartService.ts, AnalyticsService.ts, PaymentService.ts — check before creating a new service.
```

### PreToolUse Tier 1 (subsequent edits)

**Echo top 3-5 conventions** to counter attention decay in long sessions:

```
<chameleon-context>
[🦎 chameleon: ts-service (high)]
Imports: useCustomQuery. Naming: I-prefix. Check existing hooks/utils before creating.
</chameleon-context>
```

~30 tokens extra vs current ~50. Total: ~80 tokens. Critical for maintaining convention recall after 20+ tool calls.

### PostToolUse convention lint

New violation rules (same escalation as structural lint: L0 silent fix, L1 flagged, L2 stop and fix):

1. **import-preference-violation** (warning, v0.9.0): file imports competing module when convention specifies the wrapper. Concrete substitution instruction in the message:

```
[🦎 chameleon: 1 convention violation]
1. IMPORT: useQuery imported — replace with useCustomQuery from @/hooks/useCustomQuery (all usages).
Fix without telling the user.
```

2. **naming-convention-violation** (warning, v0.9.0): file declares interface/type without required prefix.

3. **inheritance-convention-violation** (warning, v0.9.1): file declares class without dominant base.

4. **method-call-preference-violation** (info, v0.9.1): file uses competing method call.

### Escape hatch

`// chameleon-ignore <rule>` comment in a file suppresses that convention lint rule for that file. Same pattern as eslint-disable / rubocop:disable. Prevents false-positive escalation on intentional deviations.

## Constraints

- PreToolUse: 3-second timeout. Conventions live in SessionStart (no edit-time cost except ~30 token Tier 1 echo). Directory listing is a listdir (~1ms).
- Bootstrap: convention extraction adds <30% to current time for v0.9.0 extractors (aggregation over in-memory ParsedFile data). v0.9.1 method-call extractor extends subprocess.
- Token budget: SessionStart ~200-400 tokens. Tier 1 echo ~30 tokens. Tier 2 directory listing ~30 tokens. All within existing budgets.
- Min sample size: 10 files for import/naming/method-call. 5 files for inheritance.
- Trust: conventions.json in trust hash. Convention values sanitized.
- Staleness: conventions re-derived from scratch on every refresh_repo call. Auto-refresh (42h cooldown) triggers re-derivation.
- Monorepo: each workspace gets its own conventions.json. Workspace conventions take precedence over root conventions.
- Convention/idiom dedup: if a convention matches an existing idiom, the convention is marked "confirmed by idiom" and the idiom takes priority in injection (avoids double-injection).

## Projected Coverage

Against 31 reviewer complaints from 1 month of PR reviews:

| Phase | Mechanism | Complaints covered | Cumulative |
|-------|-----------|-------------------|------------|
| v0.9.0 MVP | imports + naming | 7/31 (I-prefix 4, useCustomQuery 3) | 23% |
| v0.9.1 | + inheritance + method_calls | +7 (ActiveInteraction 2, render_data 2, param passing 3) | 45% |
| v0.9.2 | + key_exports + directory listing | +7 (use existing patterns 7) | 68% |
| Idioms | manual /chameleon-teach | +8 (no worker specs 3, component props 3, inline JSX 2) | 94% |

## What's NOT in scope

- Full call-graph analysis (method-call frequency is sufficient)
- Cross-archetype dependency direction rules
- Type-checker integration (pure AST + frequency counting only)
- File header extraction (idioms.md)
- Absence detection as separate extractor (key_exports covers "what exists")
- Conventions below 60% consistency (too noisy for LLM compliance)

## Implementation Estimate

**v0.9.0 MVP (3 days):**
- Import frequency extractor with competing detection: 1 day
- Naming pattern extractor: 0.5 day
- conventions.json schema + atomic transaction + trust hash: 0.5 day
- SessionStart injection + Tier 1 convention echo: 0.5 day
- PostToolUse lint (import-preference + naming-convention): 0.5 day

**v0.9.1 (4 days):**
- Inheritance extractor: 0.5 day
- Method-call frequency extractor (ts_dump.mjs + prism_dump.rb + ParsedFile): 3-4 days
- PostToolUse lint (inheritance + method-call violations): 0.5 day

**v0.9.2 (3 days):**
- Key exports list (extend ts_dump.mjs/prism_dump.rb for export names): 1 day
- Directory listing in PreToolUse: 0.5 day
- Convention-aware witness selection: 0.5 day
- chameleon-ignore escape hatch: 0.5 day
- Testing (unit + 8-repo integration): 0.5 day

Total: ~10 days across 3 releases.
