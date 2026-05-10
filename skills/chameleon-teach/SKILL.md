---
name: chameleon-teach
description: Use when the user explicitly invokes /chameleon-teach to capture a team idiom, banned import, mandatory wrapper, or pattern that AST analysis cannot infer
---

# /chameleon-teach

Append a captured team idiom to `.chameleon/idioms.md`. The load-bearing tool for **Tier 2** dimensions — patterns AST analysis fundamentally cannot detect (prohibitions, mandatory wrappers, domain vocabulary, library version constraints, etc.).

## When to use

The user identifies a missed pattern, typically after:

- Reviewer feedback on an AI-generated edit ("we don't use `useQuery` directly; use `useCustomQuery`")
- A noticed banned import (`"never import lodash directly; method-scope only"`)
- A mandatory wrapper not detected by clustering (`"all DB calls go through withTransaction"`)
- Domain vocabulary preferences (`"we say Listing, not Property"`)
- Library version constraints (`"react-router-dom is locked at v5.x; do not upgrade"`)

## The flow

1. Confirm the user is in a TypeScript repo with `.chameleon/profile.json` present.
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
5. The tool sanitizes input (strips ANSI/zero-width), enforces 50KB cap, appends under `## active` header.
6. Confirm to user: idiom added; will surface in next `/chameleon-status`.

## Idiom format examples

```markdown
### use-custom-query
Status: active (added 2026-05-10)
Use `useCustomQuery` from `~/hooks/useCustomQuery`, not `useQuery` directly.
Reason: shared error handling and retry logic.
```

```markdown
### lodash-method-scope
Status: active (added 2026-05-10)
Import lodash methods individually: `import { debounce } from 'lodash/debounce'`.
Never `import _ from 'lodash'` (whole-library import).
Reason: bundle size; tree-shaking doesn't work with namespace imports.
```

```markdown
### no-react-router-v6
Status: active (added 2026-05-10)
We are locked at react-router-dom@5.x. Do not migrate to v6 syntax
(`<Routes>`, `useNavigate`, etc.). Use v5 (`<Switch>`, `useHistory`).
Reason: in-progress migration scoped for Q4 2026; mid-migration is worse than waiting.
```

## What to ask the user

If the user gives you fragments, ask one clarifying question at a time:

> What was generated that wasn't right? (paste the code or describe it)

> What should it have been instead?

> Why does the team prefer that? (one sentence)

Don't make up reasons. If the user can't articulate why, write `Reason: team convention.`

## Anti-patterns

**Don't** call `teach_profile` with vague descriptions:

> ❌ "Use better imports" (which imports? to where?)
> ❌ "Don't write bad code" (no actionable rule)
> ❌ "Match our style" (the profile already captures style)

**Don't** call `teach_profile` for things AST already detects:

> ❌ "Use 2-space indent" — that's in `.prettierrc`; auto-derived to `rules.json`
> ❌ "PascalCase component names" — that's in the cluster signature; auto-detected
> ❌ "Files end with `.tsx`" — that's the path pattern; auto-bucketed

If you're not sure whether something is auto-detected, run `/chameleon-status` first.

## Deprecation (Phase 4)

When an idiom becomes stale (the team has migrated past it), the user can mark it deprecated:

```markdown
### use-query-direct
Status: deprecated 2026-05-10 (replaced by use-custom-query)
Reason: bypasses our shared error handling.
Migration: replace `useQuery(...)` with `useCustomQuery(...)`.
```

Phase 4 adds explicit `/chameleon-teach --deprecate <slug>` command. Phase 2D requires manual editing of `idioms.md`.
