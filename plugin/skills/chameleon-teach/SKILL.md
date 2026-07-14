---
name: chameleon-teach
argument-hint: "[pattern description]"
description: Use when the user explicitly invokes /chameleon-teach to capture a team idiom, banned import, mandatory wrapper, or pattern that AST analysis cannot infer
---

# /chameleon-teach

Append a captured team idiom to `.chameleon/idioms.md`. The load-bearing tool
for **Tier 2** dimensions — patterns AST analysis fundamentally cannot detect
(prohibitions, mandatory wrappers, domain vocabulary, library version
constraints, etc.).

Supports two capture modes:

- **Free-form** — the user describes the pattern in prose; skill formats it
  and calls `teach_profile`. (Original behavior; unchanged.)
- **Structured** — the user supplies discrete fields (`slug:`, `rationale:`,
  `example:`, `counterexample:`); skill calls `teach_profile_structured`
  which validates each field and renders the canonical idiom layout.
- **Wrapper-preference** — the user states a "use X, not Y" import rule for an
  archetype ("import our `http` wrapper, not raw `axios`"); skill calls
  `teach_competing_import`, which writes a structured `competing` convention
  the lint engine + SessionStart block then enforce (not just a prose idiom).

## When to use

The user identifies a missed pattern, typically after:

- Reviewer feedback on an AI-generated edit ("we don't use `useQuery`
  directly; use `useCustomQuery`")
- A noticed banned import (`"never import lodash directly; method-scope only"`)
- A mandatory wrapper not detected by clustering
  (`"all DB calls go through withTransaction"`)
- Domain vocabulary preferences (`"we say Listing, not Property"`)
- Library version constraints (`"react-router-dom is locked at v5.x;
  do not upgrade"`)

## Choosing the mode

If the user's request contains explicit `slug:`, `rationale:`, `example:`,
or `counterexample:` lines → use the **structured** path.

Otherwise → use the **free-form** path.

### Detecting the structured form

The structured form looks like:

```
slug: use-custom-query
rationale: Prefer useCustomQuery over useQuery for shared retry + error handling.
example: const { data } = useCustomQuery({ key: 'foo' });
counterexample: const { data } = useQuery({ key: 'foo' });
archetype: react-component
source: reviewer feedback on PR #482
```

Each field is one line (or a fenced code block for example/counterexample).
`archetype`, `status`, and `source` are optional. If the user provides AT
LEAST `slug:` + `rationale:`, use the structured path; otherwise fall back
to free-form.

## The free-form flow (original)

1. Confirm the user is in a repo with `.chameleon/profile.json` present.
2. Ask the user to describe the missed pattern. Probe for:
   - **What was generated** (the off-pattern code)
   - **What should have been generated** (the team's idiom)
   - **Why** (one sentence — the reason the team adopted this convention)
   - **Where it came from** (a PR/review comment, a doc, a file:line — see
     Honesty Rules; skip the line if the user has no provenance to give)
3. Write the idiom in this format:

   ```markdown
   ### <slug>
   Status: active (added YYYY-MM-DD)
   Source: <path:line, PR/review reference, or short provenance note>
   <what should be done; one to three sentences>
   Reason: <why; one sentence>
   ```

   For example, an idiom captured from live review feedback:

   ```markdown
   ### use-custom-query
   Status: active (added 2026-05-10)
   Source: reviewer feedback on PR #482
   Use `useCustomQuery` from `~/hooks/useCustomQuery`, not `useQuery` directly.
   Reason: shared retry + error handling; useQuery bypasses both.
   ```

4. Call `chameleon-mcp::chameleon_lifecycle(action="teach_profile", params={"repo": <repo_path>, "feedback": <formatted idiom>})`.
5. The tool sanitizes input (strips ANSI/zero-width), enforces 50KB cap,
   appends under `## active` header.
6. Confirm to user: idiom added; will surface in next `/chameleon-status`.

## The structured flow

1. Confirm the user is in a repo with `.chameleon/profile.json` present.
2. Parse the user's request into `slug`, `rationale`, `example`,
   `counterexample`, optional `archetype`, optional `status`, optional
   `source` (a `source:` line — a path:line, a git ref, or a short
   provenance note). Strip fenced code blocks of their fences when
   extracting example/counterexample bodies (you want the raw code, not
   the ``` markers).
3. Call:

   ```
   chameleon-mcp::chameleon_lifecycle(
     action="teach_profile_structured",
     params={
       "repo": <abs-repo-path>,
       "slug": <slug>,
       "rationale": <rationale>,
       "example": <example or None>,
       "counterexample": <counterexample or None>,
       "archetype": <archetype or None>,
       "status": <status or "active">,
       "source": <source or None>,
     },
   )
   ```

   For example, capturing an idiom sourced from a review comment:

   ```
   chameleon-mcp::chameleon_lifecycle(
     action="teach_profile_structured",
     params={
       "repo": "/Users/you/repo",
       "slug": "use-custom-query",
       "rationale": "Prefer useCustomQuery over useQuery for shared retry + error handling.",
       "example": "const { data } = useCustomQuery({ key: 'foo' });",
       "counterexample": "const { data } = useQuery({ key: 'foo' });",
       "archetype": "react-component",
       "status": "active",
       "source": "reviewer feedback on PR #482",
     },
   )
   ```

4. The tool:
   - Validates `slug` against `^[a-z][a-z0-9-]{2,63}$`
   - Validates `archetype` (if present) against the archetype name regex
   - Validates `status ∈ {active, deprecated}`
   - Enforces a 50KB cap on `rationale + example + counterexample + source`
   - Renders to `.chameleon/idioms.md` in the canonical idiom layout
   - Inherits the sanitization, advisory lock, placeholder-strip path
     from `teach_profile`
5. If the response is `status: failed`, surface the error and ask the
   user to fix the offending field. Do not retry silently with a mangled
   slug — the user's intent matters.

### Validation failure messages — handle each cleanly

| Error contains | Action |
|---|---|
| `must match` (on the slug) | The engine interpolates the offending value: `slug 'ab' must match '^[a-z]...'`, so the literal `slug must match` is NOT a contiguous substring — match on `must match`. Tell user: slug must be 3-64 chars, lowercase letters/digits/hyphens, start with a letter |
| `already exists in '## active'` | The slug is already an ACTIVE idiom. Teaching is append-only, so this is a rejection, not an overwrite: pick a new slug, or (if the user wants to replace it) deprecate the old one first (see Deprecation) then teach the new one |
| `already exists in '## deprecated'` | The slug was deliberately deprecated. Pick a new slug rather than reviving the deprecated name silently |
| `rationale is required` | Ask user for a one-sentence reason |
| `50KB cap` | Tell user: rationale + example + counterexample + source together must be < 50KB; trim the longest field |
| `status must be` | Tell user: status must be `active` or `deprecated` (default: active) |
| `archetype` regex error | Tell user: archetype names must be 1-64 chars, lowercase letters/digits/hyphens, start with a letter (min length differs from slugs) |
| `no profile in this repo` | Tell user to run `/chameleon-init` first |

## The wrapper-preference flow

When the user states a banned-import / mandatory-wrapper rule scoped to an
archetype ("use `@/lib/http`, not `axios`"), capture it as a structured
`competing` convention instead of a prose idiom — it then drives the
`import-preference` lint rule and the "use the project's wrapper" principle:

```
chameleon-mcp::chameleon_lifecycle(
  action="teach_competing_import",
  params={
    "repo": <abs-repo-path>,
    "archetype": <archetype name>,
    "preferred": <the wrapper/module to use>,
    "over": <the raw module to avoid>,
  },
)
```

The tool validates the archetype name, requires non-empty distinct
`preferred`/`over`, and writes `conventions.imports.<archetype>.competing` in
place (flock-serialized). The profile hash changes, so tell the user to run
`/chameleon-trust` if it shows as stale.

## Idiom format examples

### Structured (preferred when capturing fresh idioms in code review)

```markdown
### use-custom-query
Status: active (added 2026-05-10)
Archetype: react-component
Source: reviewer feedback on PR #482
Use `useCustomQuery` from `~/hooks/useCustomQuery`, not `useQuery` directly.

Example:
```
const { data } = useCustomQuery({ key: 'foo' });
```

Counterexample:
```
const { data } = useQuery({ key: 'foo' });
```
```

### Free-form (when the user can't articulate fields cleanly)

```markdown
### lodash-method-scope
Status: active (added 2026-05-10)
Source: reviewer feedback on PR #418
Import lodash methods individually: `import { debounce } from 'lodash/debounce'`.
Never `import _ from 'lodash'` (whole-library import).
Reason: bundle size; tree-shaking doesn't work with namespace imports.
```

## What to ask the user

If the user gives you fragments, ask one clarifying question at a time:

> What was generated that wasn't right? (paste the code or describe it)

> What should it have been instead?

> Why does the team prefer that? (one sentence)

Don't make up reasons. If the user can't articulate why, write
`Reason: team convention.`

## Anti-patterns

**Don't** call either teach tool with vague descriptions:

> "Use better imports" (which imports? to where?)
> "Don't write bad code" (no actionable rule)
> "Match our style" (the profile already captures style)

**Don't** call either teach tool for things AST already detects:

> "Use 2-space indent" — that's in `.prettierrc`; auto-derived to `rules.json`
> "PascalCase component names" — that's in the cluster signature; auto-detected
> "Files end with `.tsx`" — that's the path pattern; auto-bucketed

If you're not sure whether something is auto-detected, run `/chameleon-status` first.

## Deprecation

When an idiom becomes stale, capture a new idiom with `status="deprecated"`:

Structured:
```
slug: use-query-direct
status: deprecated
rationale: bypasses our shared error handling; replaced by use-custom-query
```

This renders as:

```markdown
### use-query-direct
Status: deprecated 2026-05-10
bypasses our shared error handling; replaced by use-custom-query
```

The structured path makes deprecation a first-class operation. Phase 4
will add explicit `/chameleon-teach --deprecate <slug>` shorthand.

**Deprecating an EXISTING active idiom overwrites its body.** When the slug is
already in `## active`, `status="deprecated"` moves the block to
`## deprecated` and replaces its rationale/example/counterexample with
whatever you pass — it does not preserve the original text. If you want to
keep the original guidance for the record, read the active block from
`idioms.md` first and re-pass its body (append a deprecation note to the
rationale), rather than passing only a short "replaced by X" line.

## Honesty Rules

- Capture only a real rule: one observed in the repo or stated by the user. Never invent an archetype name, a wrapper, or a banned import that does not exist; grep or read before naming it.
- Record the rationale truthfully and the `source` provenance where the rule came from; don't dress up a guess as a derived convention.
- Teaching a NEW idiom is append-only, and it never SILENTLY edits an existing one: re-teaching a live slug is rejected (`already exists in '## active'`), not an overwrite. The one path that rewrites an existing block is an explicit `status="deprecated"` on an active slug, which moves it to `## deprecated` AND replaces its body with whatever you pass (see Deprecation) — a deliberate, user-driven action, never an accident. Run `chameleon_telemetry(action="check_idiom_candidates", ...)` to avoid duplicating or contradicting one already captured.
- Don't claim a taught rule is enforced: a captured idiom shapes guidance and review; only calibrated block rules deny an edit.
