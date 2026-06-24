---
name: chameleon-init
description: Use when the user explicitly invokes /chameleon-init to bootstrap a chameleon profile for the current repository (TypeScript/JavaScript, Ruby, or Python — framework-agnostic, with deeper awareness for Rails and Django/DRF/Flask/FastAPI)
---

# /chameleon-init

Bootstrap a chameleon profile for the current repo, then auto-apply
rename proposals so the archetype labels match the team's vocabulary
without a user interview. Profile artifacts are written atomically via
the commit-marker pattern.

## When to use

User runs `/chameleon-init` in a TypeScript/JavaScript, Ruby, or Python repo that
does not yet have a `.chameleon/` directory. chameleon supports these three as
first-class languages and is framework-agnostic by default — it learns the repo's
own conventions, so any framework works — with deeper, framework-aware guidance
where conventions are strong (Rails for Ruby; Django, DRF, Flask, FastAPI for Python).

If `.chameleon/profile.json` already exists, suggest `/chameleon-refresh` instead —
running init twice would overwrite the existing profile.

## The flow (overall)

1. Confirm the repo's language: TypeScript/JavaScript (`tsconfig.json` or TS in `package.json` deps),
   Ruby (`Gemfile`, or `config/application.rb` for Rails), or Python
   (`pyproject.toml` / `setup.py` / `requirements.txt` / `manage.py`, or any `.py` files).
2. **Determine the production branch** (the branch the profile derives
   from, regardless of what is checked out). Call
   `chameleon-mcp::detect_repo(file_path=<repo_root>)` and read its
   `production_branch` block:
   - `locked: true` — a lock already exists; just mention it.
   - clean detection (`branch` set, `conflict: false`, `from_origin: true`)
     — zero-touch: announce "production branch: `<branch>` (auto-detected
     from the origin default; will be locked)". Do NOT ask.
   - `conflict: true` — ask ONE short question: "Which branch is
     production? (detected: `<branch>`, also found: `<candidates>`)". Pass
     the answer as `bootstrap_repo(production_ref=<answer>)`.
   - no branch / `from_origin: false` — local-only or unrecognized layout.
     Ask once: "Which branch should chameleon treat as production? (Enter
     to skip — the working tree will be analyzed instead)". Pass a
     non-empty answer via `production_ref`; on skip just proceed.
3. Call `chameleon-mcp::bootstrap_repo(path=<repo_root>)` (plus
   `production_ref=<answer>` when step 2 asked). With a lock, the pipeline
   analyzes the production branch's tree — a detached materialization of
   the locked ref — NOT the current checkout; feature-branch noise never
   shapes the profile. Without one it analyzes the working tree as before.
4. The tool runs the full pipeline (workspace detection, tool config reading,
   discovery, AST parse, clustering, canonical selection, atomic profile
   commit). Archetypes start out with heuristic names like `controller`,
   `react-component`, `service`, `migration`.
5. **Auto-apply rename proposals** (see below) so cluster-* / class-* /
   numeric-suffix fallback names get replaced with the team's vocabulary.
6. Report the BootstrapReport to the user: archetype count, files
   processed, duration, profile path, the production-branch lock (the
   envelope's `production_ref` block), and what got renamed.
7. Suggest the user run `/chameleon-trust` to approve the profile for
   their session.

## Default: auto-apply renames (no user interview)

Renames are purely cosmetic — they just rekey archetypes.json /
canonicals.json / rules.json / idioms.md. Pattern quality, witness
selection, and lint behavior are unaffected. So auto-rename is the
default: the skill picks the best candidate per
archetype from `propose_archetype_renames` and applies them without
asking.

### Auto-apply algorithm

1. Call `propose_archetype_renames(repo=<abs-repo-path>, top_n=16)`.
2. For each proposal, decide whether to rename:
   - **Always rename** when the current name is a low-information
     fallback: starts with `cluster-` (raw hash), starts with `class-`
     (generic Ruby class name when the witness is more specific), or
     ends in a bare numeric disambiguator like `-2` / `-3` with no
     semantic suffix.
   - **Skip** when the current name is already descriptive
     (`controller`, `service`, `model`, `migration`, `worker`,
     `react-component`, `next-page`, etc.) AND the top suggested
     alternative isn't materially better.
   - **Tie-break** on multiple candidates: prefer the alternative the
     proposal ranks first (`suggested_alternatives[0]`), unless it
     duplicates an existing archetype name in the same profile (skip
     in that case).
3. Validate every chosen new name against `\A[a-z][a-z0-9-]{0,63}\Z`.
   Discard any invalid pick silently.
4. Apply the resulting `{old: new}` map via
   `apply_archetype_renames(repo=<abs-repo-path>, renames=...)`.
5. Report what got renamed in the BootstrapReport summary (e.g.
   "renamed 3 archetypes: cluster-25874012 → test-models-spec,
   class-foo → service-foo, controller-2 → admin-controller").

### When to skip auto-apply entirely

- `propose_archetype_renames` returns `status: failed` (no profile, etc.)
  → skip silently.
- The proposal returns zero archetypes that meet the "always rename"
  bar → skip silently, report "renames applied: 0".
- The repo's `.chameleon/config.json` sets `auto_rename: false` → fall
  back to the legacy ≤3-prompt interview below.

### Legacy ≤3-prompt interview (only when `auto_rename: false`)

If `.chameleon/config.json` is present AND sets `auto_rename: false`,
run the interactive interview:

- Prompt 1: list top-8 archetypes, ask "any to override?"
- Prompt 2: per picked archetype, show `suggested_alternatives` and
  let the user pick a number, type a custom name, or "keep".
- Prompt 3: confirm the final mapping, then `apply_archetype_renames`.

Invalid names get one re-ask with the regex hint
(`\A[a-z][a-z0-9-]{0,63}\Z`). The interview is strictly ≤3 prompts.

## What to tell the user before running bootstrap

> chameleon will scan the production branch's tree (when a production
> branch is locked or auto-detected — your current checkout doesn't have to
> be on it), cluster the files into archetypes (e.g. "next-server-component",
> "service", "controller", "rails-controller"),
> and pick a canonical example for each. After bootstrap, archetype renames are applied automatically.
> It will write a
> `.chameleon/` directory you should commit. This usually takes under 10
> seconds for repos under 5,000 files; under 1 minute for repos up to
> 200,000 files. No LLM cost, no network (the production tree comes from
> your local git objects, current as of your last fetch).

If the repo has > 50,000 source files, the tool refuses by default. Ask
the user for an explicit `paths_glob` (e.g., `src/**/*.ts` or `app/**/*.rb`).

## Common failure modes

| Failure | Action |
|---|---|
| `failed_unsupported_language` | No TypeScript/JavaScript, Ruby, or Python signals (no tsconfig, no Gemfile, no pyproject/setup.py/`.py`). Tell the user chameleon supports TypeScript/JavaScript, Ruby, and Python as first-class languages — framework-agnostic, with deeper awareness for Rails and Django/DRF/Flask/FastAPI; other languages are not yet supported. |
| `failed_too_many_files` | Repo exceeds 200k file ceiling. Ask user for `paths_glob` to scope. |
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
- Production branch: <branch> (locked — derivation pinned to <ref> @ <sha12>)
- Renames applied: K  (auto-rename)

Next steps:
1. Run /chameleon-trust to approve this profile for your user.
2. Commit `.chameleon/` to git so your team can share the profile.
3. Run /chameleon-auto-idiom to derive team idioms (mandatory wrappers,
   domain vocabulary, auth invariants) from repo evidence — a fresh profile
   has none, and AST analysis cannot infer them.
4. (Optional) Run /chameleon-teach to capture specific idioms by hand.
```

A fresh bootstrap always starts with zero idioms, so the `/chameleon-auto-idiom`
offer in step 3 is unconditional: ask the user whether to run it now. If they
accept, invoke the `chameleon-auto-idiom` skill in this same session. It is
append-only — it never touches idioms the team later adds.

## Out of scope (current release)

- Languages other than TypeScript/JavaScript, Ruby, and Python — `failed_unsupported_language`.
  Future releases may add Go, etc.
- Per-workspace bootstrapping in monorepos — current implementation
  bootstraps at repo root regardless. Future versions will add per-workspace
  `.chameleon/` directories.
- Renaming archetypes outside the top-N by cluster size — the interview
  only surfaces the largest ones because the long tail is rarely worth
  retitling. Users can re-run /chameleon-init or edit `.chameleon/archetypes.json`
  directly for the long tail.
