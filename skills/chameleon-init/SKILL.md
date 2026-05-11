---
name: chameleon-init
description: Use when the user explicitly invokes /chameleon-init to bootstrap a chameleon profile for the current repository (TypeScript or Ruby on Rails)
---

# /chameleon-init

Bootstrap a chameleon profile for the current repo, then drive a short
≤3-prompt interview so the user can rename auto-generated archetype labels
to something their team will recognize. Profile artifacts are written
atomically via the commit-marker pattern.

## When to use

User runs `/chameleon-init` (or `/cham-init`) in a TypeScript or Ruby on Rails
repo that does not yet have a `.chameleon/` directory.

If `.chameleon/profile.json` already exists, suggest `/chameleon-refresh` instead —
running init twice would overwrite the existing profile.

## The flow (overall)

1. Confirm the repo's language: TypeScript (`tsconfig.json` or TS in `package.json` deps)
   or Ruby on Rails (`Gemfile` with rails, or `config/application.rb`).
2. Call `chameleon-mcp::bootstrap_repo(path=<repo_root>)`.
3. The tool runs the full pipeline (workspace detection, tool config reading,
   discovery, AST parse, clustering, canonical selection, atomic profile
   commit). Archetypes start out with heuristic names like `controller`,
   `react-component`, `service`, `migration`.
4. Run the **≤3-prompt rename interview** (below) so the user can override
   any name that doesn't match their team's vocabulary.
5. Report the BootstrapReport to the user: archetype count, files
   processed, duration, profile path.
6. Suggest the user run `/chameleon-trust` to approve the profile for
   their session.

## The ≤3-prompt rename interview (after bootstrap succeeds)

The interview is a strict three-prompt protocol. Do not exceed three
user-facing prompts. The MCP exposes two stateless tools the skill drives:

- `propose_archetype_renames(repo, top_n=8)` — returns the top-N largest
  archetypes plus 3-5 candidate names per archetype.
- `apply_archetype_renames(repo, renames)` — atomically rewrites
  archetypes.json + canonicals.json + profile.summary.md keys.

### Prompt 1 — "Any names to override?"

Call `propose_archetype_renames(repo=<abs-repo-path>, top_n=8)`. Format
the response as a numbered list and ask the user (verbatim or close):

> Bootstrap found these archetypes (largest first). Any names you'd like
> to override?
>
> 1. **controller** (12 files) — canonical `app/controllers/users_controller.rb`
> 2. **react-component** (89 files) — canonical `src/components/Button.tsx`
> 3. **service** (8 files) — canonical `app/services/UserCreator.rb`
> ...
>
> Reply with the numbers you want to rename (e.g. "2, 4"), or "no" to
> keep the auto-derived names.

If the user says "no" / "skip" / "all good" → skip to **Prompt 3**.

### Prompt 2 — "What should each become?"

For each number the user picked, show the **suggested_alternatives**
list from the propose response and ask:

> For #2 (**react-component**), suggested alternatives:
> - react-component (current)
> - button
> - components
> - class
> - react-component-button
>
> Pick a number, type a custom name, or "keep" to leave it as-is.

Repeat per archetype the user picked. Collect the responses into a single
`{old_name: new_name}` dict. Skip entries where the user picks "keep" or
types the existing name (no-op).

If the user types an invalid name (uppercase letters, leading digit,
underscores, spaces) silently re-ask with a one-line correction:

> Names must match `[a-z][a-z0-9-]{0,63}` — try again?

### Prompt 3 — Confirm + apply

Show the user the final mapping and ask for confirmation:

> About to rename:
> - react-component → button
> - service → user-orchestrator
>
> Apply? (y/n)

On "y": call `apply_archetype_renames(repo=<abs-repo-path>, renames={...})`.
On "n" or empty mapping: skip the apply, move straight to the trust step.

Then report `renames_applied` + suggest `/chameleon-trust`.

### What if propose returns nothing useful?

If `propose_archetype_renames` returns `status: failed` (no profile, etc.),
silently skip the rename interview and continue to the trust step. The
auto-derived names are good enough.

## What to tell the user before running bootstrap

> chameleon will scan your repo's source files, cluster them into archetypes
> (e.g. "next-server-component", "service", "controller", "rails-controller"),
> and pick a canonical example for each. After bootstrap I'll ask up to 3
> short questions if you want to rename anything. It will write a
> `.chameleon/` directory you should commit. This usually takes under 10
> seconds for repos under 5,000 files; under 1 minute for repos up to
> 50,000 files. No LLM cost.

If the repo has > 50,000 source files, the tool refuses by default. Ask
the user for an explicit `paths_glob` (e.g., `src/**/*.ts` or `app/**/*.rb`).

## Common failure modes

| Failure | Action |
|---|---|
| `failed_unsupported_language` | No TypeScript or Ruby signals (no tsconfig, no Gemfile). Tell the user chameleon currently supports TS + Rails; other languages are not yet supported. |
| `failed_too_many_files` | Repo exceeds 50k file ceiling. Ask user for `paths_glob` to scope. |
| Bootstrap completes but `archetypes_detected == 0` | All clusters were sparse (< 5 files). User likely has a tiny project; suggest manual archetype curation via `/chameleon-teach`. Skip the rename interview. |
| `canonicals_skipped_failed_scans > 0` | Some clusters had every candidate fail secret/injection/poisoning scans. Tell the user to investigate via `/chameleon-status`. |
| `apply_archetype_renames` returns `failed` | Surface the error verbatim and ask the user to retry with a corrected mapping. Do NOT re-bootstrap. |

## After success

```
Profile created at .chameleon/
- Archetypes detected: N
- Rules extracted: M
- Files processed: X (Y skipped: generated, Z skipped: parse errors)
- Duration: Tms
- Renames applied: K  (from interview)

Next steps:
1. Run /chameleon-trust to approve this profile for your user.
2. Commit `.chameleon/` to git so your team can share the profile.
3. (Optional) Run /chameleon-teach to capture team idioms (banned imports,
   mandatory wrappers, etc.) that AST analysis cannot infer.
```

## Out of scope (current release)

- Languages other than TypeScript and Ruby — `failed_unsupported_language`.
  Future releases may add Python, Go, etc.
- Per-workspace bootstrapping in monorepos — current implementation
  bootstraps at repo root regardless. Future versions will add per-workspace
  `.chameleon/` directories.
- Renaming archetypes outside the top-N by cluster size — the interview
  only surfaces the largest ones because the long tail is rarely worth
  retitling. Users can re-run /chameleon-init or edit `.chameleon/archetypes.json`
  directly for the long tail.
