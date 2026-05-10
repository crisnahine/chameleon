---
name: chameleon-init
description: Use when the user explicitly invokes /chameleon-init to bootstrap a chameleon profile for the current repository (TypeScript or Ruby on Rails)
---

# /chameleon-init

Bootstrap a chameleon profile for the current repo. Generates `.chameleon/profile.json` and friends, written atomically via the commit-marker pattern.

## When to use

User runs `/chameleon-init` (or `/cham-init`) in a TypeScript or Ruby on Rails repo that does not yet have a `.chameleon/` directory.

If `.chameleon/profile.json` already exists, suggest `/chameleon-refresh` instead — running init twice would overwrite the existing profile.

## The flow

1. Confirm the repo's language: TypeScript (`tsconfig.json` or TS in `package.json` deps) or Ruby on Rails (`Gemfile` with rails, or `config/application.rb`).
2. Call `chameleon-mcp::bootstrap_repo(path=<repo_root>)`.
3. The tool runs the full pipeline:
   - Workspace detection (pnpm/yarn/lerna/turbo/nx for TS; non-monorepo for Rails)
   - Tool config reading (`.prettierrc`, `tsconfig.json`, `.eslintrc*` for TS; `.rubocop.yml` for Ruby)
   - File discovery + 50k ceiling enforcement
   - AST parse via `ts_dump.mjs` (TypeScript) or `prism_dump.rb` (Ruby)
   - Cluster signature computation
   - Canonical selection (with secret + injection scans)
   - Atomic profile commit
4. Report the BootstrapReport to the user: archetype count, files processed, duration, profile path.
5. Suggest the user run `/chameleon-trust` to approve the profile for their session.

## What to tell the user before running

> chameleon will scan your repo's source files, cluster them into archetypes (e.g. "next-server-component", "service", "controller", "rails-controller"), and pick a canonical example for each. It will write a `.chameleon/` directory you should commit. This usually takes under 10 seconds for repos under 5,000 files; under 1 minute for repos up to 50,000 files. No LLM cost.

If the repo has > 50,000 source files, the tool refuses by default. Ask the user for an explicit `paths_glob` (e.g., `src/**/*.ts` or `app/**/*.rb`).

## Common failure modes

| Failure | Action |
|---|---|
| `failed_unsupported_language` | No TypeScript or Ruby signals (no tsconfig, no Gemfile). Tell the user chameleon currently supports TS + Rails; other languages are not yet supported. |
| `failed_too_many_files` | Repo exceeds 50k file ceiling. Ask user for `paths_glob` to scope. |
| Bootstrap completes but `archetypes_detected == 0` | All clusters were sparse (< 5 files). User likely has a tiny project; suggest manual archetype curation via `/chameleon-teach`. |
| `canonicals_skipped_failed_scans > 0` | Some clusters had every candidate fail secret/injection/poisoning scans. Tell the user to investigate via `/chameleon-status`. |

## After success

```
Profile created at .chameleon/
- Archetypes detected: N
- Rules extracted: M
- Files processed: X (Y skipped: generated, Z skipped: parse errors)
- Duration: Tms
- Cost: ~$XX

Next steps:
1. Run /chameleon-trust to approve this profile for your user.
2. Commit `.chameleon/` to git so your team can share the profile.
3. (Optional) Run /chameleon-teach to capture team idioms (banned imports,
   mandatory wrappers, etc.) that AST analysis cannot infer.
```

## Out of scope (current release)

- Languages other than TypeScript and Ruby — `failed_unsupported_language`. Future releases may add Python, Go, etc.
- Per-workspace bootstrapping in monorepos — current implementation bootstraps at repo root regardless. Future versions will add per-workspace `.chameleon/` directories.
- Interactive ≤3-prompt interview — current implementation is non-interactive bootstrap with auto-generated archetype names (`cluster-<hash>`). Future versions wrap this with the interview to rename archetypes.
