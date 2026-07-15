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
   (any `.py` source files — `setup.py` counts). `pyproject.toml` / `requirements.txt` /
   `manage.py` / `Pipfile` mark a Python framework, but the repo needs actual `.py`
   source to profile: a manifest-only repo with no `.py` files has nothing to cluster
   and bootstrap returns `failed_unsupported_language`, so don't announce it as
   ready-to-bootstrap on a manifest alone.
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
     the answer as `"production_ref": <answer>` in the bootstrap call's
     `params` (step 3).
   - no branch / `from_origin: false` — local-only or unrecognized layout.
     Ask once: "Which branch should chameleon treat as production? (Enter
     to skip — the working tree will be analyzed instead)". Pass a
     non-empty answer via `production_ref`; on skip just proceed.
3. Call `chameleon-mcp::chameleon_lifecycle(action="bootstrap_repo", params={"path": <repo_root>})`
   (plus `"production_ref": <answer>` in `params` when step 2 asked). With a lock, the pipeline
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

1. Call `chameleon-mcp::chameleon_lifecycle(action="propose_archetype_renames", params={"repo": <abs-repo-path>, "top_n": 16})`.
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
3b. **Dedup the chosen targets against EACH OTHER**, not only against existing
   names. `apply_archetype_renames` is all-or-nothing: if two archetypes both pick
   the same new name (e.g. two test clusters both proposing `tests-py`), the whole
   batch hard-fails on the collision and NOTHING is renamed. After step 3, walk the
   picks in order and drop any whose target duplicates an already-chosen target (or
   an existing archetype name); the first claimant keeps the name, the later
   collider keeps its original. Only then build the `{old: new}` map.
4. Apply the resulting `{old: new}` map via
   `chameleon-mcp::chameleon_lifecycle(action="apply_archetype_renames", params={"repo": <abs-repo-path>, "renames": ...})`.
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
- Prompt 3: confirm the final mapping, then apply it via
  `chameleon_lifecycle(action="apply_archetype_renames", ...)`.

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

If the repo has > 200,000 source files, the tool refuses by default
(`failed_too_many_files`). Ask the user for an explicit `paths_glob` (e.g.,
`src/**/*.ts` or `app/**/*.rb`).

## Common failure modes

| Failure | Action |
|---|---|
| `failed_unsupported_language` | No TypeScript/JavaScript, Ruby, or Python signals (no tsconfig, no Gemfile, no pyproject/setup.py/`.py`). Tell the user chameleon supports TypeScript/JavaScript, Ruby, and Python as first-class languages — framework-agnostic, with deeper awareness for Rails and Django/DRF/Flask/FastAPI; other languages are not yet supported. |
| `failed_too_many_files` | Repo exceeds 200k file ceiling. Ask user for `paths_glob` to scope. |
| Bootstrap completes but `archetypes_detected == 0` | All clusters were sparse (< 5 files). User likely has a tiny project; suggest manual archetype curation via `/chameleon-teach`. Skip the rename interview. |
| `canonicals_skipped_failed_scans > 0` | Some clusters had every candidate fail secret/injection/poisoning scans. Tell the user to investigate via `/chameleon-status`. |
| `apply_archetype_renames` returns `failed` | Surface the error verbatim and ask the user to retry with a corrected mapping. Do NOT re-bootstrap. |

## Coordinator monorepo (`status: "success_workspaces_only"`)

A monorepo whose ROOT carries no first-class source (a pnpm-workspaces /
Turborepo / Nx coordinator: only a root `package.json` + `pnpm-workspace.yaml`,
all code under `apps/*` / `packages/*`) bootstraps its WORKSPACES but writes NO
root `.chameleon/` profile. The envelope's `status` is `"success_workspaces_only"`
and its `workspaces` array lists each bootstrapped workspace
(`{workspace_path, repo_root, status, archetypes_detected, ...}`).

Handle this distinctly from plain `success`:
- Do NOT tell the user to run `/chameleon-trust` at the repo ROOT — there is no
  root profile, so it fails with "no .chameleon/ directory (run /chameleon-init
  first)", which reads as a contradiction right after init succeeded. Instead,
  tell them to run `/chameleon-trust` once **per workspace** (cd into each
  `workspace_path`, or trust each), listing the workspaces from the envelope.
- Per-edit guidance (PreToolUse/PostToolUse) resolves per FILE to its workspace,
  so editing a workspace file gets that workspace's conventions once its profile
  is trusted — that part works normally.
- Claude Code launches at the repo ROOT, but the turn-end Stop safety net
  (cross-file break detection, the async review job's correctness/
  duplication/idiom lenses, stale-test advisories, the session attestation)
  still covers
  the session: the multi-root Stop backstop discovers every workspace the
  turn actually touched (from each edited file's own workspace-scoped
  enforcement state, not the launch cwd) and gates it against its own
  trusted profile, so a pure-coordinator root is not a turn-end dead spot.
  Each workspace still needs its own `/chameleon-trust` for its gate to run.

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

### Surface dropped archetypes (do not swallow the bootstrap warnings)

The bootstrap envelope carries diagnostic warning lists that name real code the
profile did NOT turn into an archetype. Read them and, when non-empty, tell the
user in one short line each — otherwise a real role silently has no guidance:

- `sparse_cluster_warnings` — a small group of same-role files (e.g. two
  `*.guard.ts` NestJS guards, one migration) fell below the cluster-size floor,
  so no archetype covers them. List each pattern and suggest
  `/chameleon-teach` to capture the role by hand if it matters.
- `bimodal_cluster_warnings` — a cluster split into two shapes; the canonical
  witness may not represent both. Worth a `/chameleon-refresh` after the tree
  settles.
- `workspace_skipped_warnings` / `workspace_glob_warnings` /
  `nested_profile_warnings` — a monorepo workspace was skipped or a glob
  matched nothing; name the path so the user can bootstrap it explicitly.

Keep it terse (the counts, not a wall of JSON). Skip a category whose list is
empty. This is advisory — none of it blocks a successful bootstrap.

### Offer the conventions memory wiring

When the bootstrap produced `.chameleon/conventions.md` (it exists whenever the
repo derived or taught renderable conventions), offer to wire it into Claude's
memory channel. Explain why in one sentence: conventions delivered through the
memory channel carry materially more instruction authority for coding agents
than hook-injected context (measured: the identical rules were followed 100%
via this channel vs 40% as a hook advisory, across TS/Ruby/Python), and
chameleon keeps the file fresh on every refresh/teach.

Offer the options in this order (all verified equivalent at 100%; all
consent-gated — create/edit only on an explicit yes, never re-ask in the same
session if declined):

1. **`.claude/rules/chameleon-conventions.md`** (default suggestion) — create
   this one-line file:

   ```
   @../../.chameleon/conventions.md
   ```

   `.claude/rules/*.md` auto-loads at session start (official memory feature),
   so the whole team is covered once it is committed, and NO existing file —
   including the repo's `CLAUDE.md` — is ever modified.

2. **`CLAUDE.local.md`** — same one-line import (`@.chameleon/conventions.md`),
   for a user who wants the wiring personal and untracked (add it to
   `.gitignore` if not already ignored). Nothing shared changes.

3. **`CLAUDE.md` import** — append `@.chameleon/conventions.md` to the repo's
   `CLAUDE.md` only if the user explicitly prefers it there. This edits the
   team's file, so make that consequence explicit before doing it.

If any of the three wirings is already present, say nothing — it is wired.

## Out of scope (current release)

- Languages other than TypeScript/JavaScript, Ruby, and Python — `failed_unsupported_language`.
  Future releases may add Go, etc.
- Renaming archetypes outside the top-N by cluster size — the interview
  only surfaces the largest ones because the long tail is rarely worth
  retitling. Users can re-run /chameleon-init or edit `.chameleon/archetypes.json`
  directly for the long tail.
