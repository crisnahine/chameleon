---
name: chameleon-teach
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
```

Each field is one line (or a fenced code block for example/counterexample).
`archetype` and `status` are optional. If the user provides AT LEAST `slug:`
+ `rationale:`, use the structured path; otherwise fall back to free-form.

## The free-form flow (original)

1. Confirm the user is in a repo with `.chameleon/profile.json` present.
2. Ask the user to describe the missed pattern. Probe for:
   - **What was generated** (the off-pattern code)
   - **What should have been generated** (the team's idiom)
   - **Why** (one sentence — the reason the team adopted this convention)
3. Write the idiom in this format:

   ```markdown
   ### <slug>
   Status: active (added YYYY-MM-DD)
   <what should be done; one to three sentences>
   Reason: <why; one sentence>
   ```

4. Call `chameleon-mcp::teach_profile(repo=<repo_path>, feedback=<formatted idiom>)`.
5. The tool sanitizes input (strips ANSI/zero-width), enforces 50KB cap,
   appends under `## active` header.
6. Confirm to user: idiom added; will surface in next `/chameleon-status`.

## The structured flow

1. Confirm the user is in a repo with `.chameleon/profile.json` present.
2. Parse the user's request into `slug`, `rationale`, `example`,
   `counterexample`, optional `archetype`, optional `status`. Strip fenced
   code blocks of their fences when extracting example/counterexample
   bodies (you want the raw code, not the ``` markers).
3. Call:

   ```
   chameleon-mcp::teach_profile_structured(
     repo=<abs-repo-path>,
     slug=<slug>,
     rationale=<rationale>,
     example=<example or None>,
     counterexample=<counterexample or None>,
     archetype=<archetype or None>,
     status=<status or "active">,
   )
   ```

4. The tool:
   - Validates `slug` against `^[a-z][a-z0-9-]{2,63}$`
   - Validates `archetype` (if present) against the archetype name regex
   - Validates `status ∈ {active, deprecated}`
   - Enforces a 50KB cap on `rationale + example + counterexample`
   - Renders to `.chameleon/idioms.md` in the canonical idiom layout
   - Inherits the sanitization, advisory lock, placeholder-strip path
     from `teach_profile`
5. If the response is `status: failed`, surface the error and ask the
   user to fix the offending field. Do not retry silently with a mangled
   slug — the user's intent matters.

### Validation failure messages — handle each cleanly

| Error contains | Action |
|---|---|
| `slug must match` | Tell user: slug must be 3-64 chars, lowercase letters/digits/hyphens, start with a letter |
| `rationale is required` | Ask user for a one-sentence reason |
| `50KB cap` | Tell user: rationale + example + counterexample together must be < 50KB; trim the longest field |
| `status must be` | Tell user: status must be `active` or `deprecated` (default: active) |
| `archetype` regex error | Tell user: archetype names must be 1-64 chars, lowercase letters/digits/hyphens, start with a letter (min length differs from slugs) |
| `no profile in this repo` | Tell user to run `/chameleon-init` first |

## Idiom format examples

### Structured (preferred when capturing fresh idioms in code review)

```markdown
### use-custom-query
Status: active (added 2026-05-10)
Archetype: react-component
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
