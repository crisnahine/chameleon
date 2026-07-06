# Verification runbook — turnkey human sign-off

> Item 1 of the Definition of Done (`docs/chameleon-goal.md`) is the one gate no
> automation can close: a human driving each golden repo through a real Claude Code
> session and recording the result. This runbook makes that gate mechanical — per
> cell, the exact action to perform, the pass signal to look for, and the negative
> (off-state) check. Record each result in `docs/verification-matrix.md`.
>
> Everything below has been pre-verified by automated scaffolding (across the four
> expert panels it found 14 real defects — G-007..G-020: 13 fixed, most pinned by
> regression tests, and one scoped out at the time and later shipped as a feature
> (G-020 → class/module search, v2.50.0); see `docs/gap-log.md`), so you are
> confirming, not debugging. If any step does NOT show the pass signal, that opens
> a gap — log it.

## How to use

1. Open the cell's golden repo in a real Claude Code session with chameleon installed.
2. Run the action. Watch the transcript / files / terminal for the pass signal.
3. Run the negative check (kill switch off, or a should-NOT-fire request).
4. Mark the (subsystem × cell) cell `PASS` or `FAIL` in `docs/verification-matrix.md`.

Golden repos (all under `~/Documents/Projects/Testing Apps/`, all bootstrapped):
C1 `excalidraw` · C4 `ef-api` · C5 `forem` · C7/C8 `py-django-readthedocs` ·
E1 `gitlabhq` · C2 `golden-ts-nextjs` · C3 `golden-ts-nestjs` ·
C6 `golden-py-plain` · C9 `py-flask-flaskbb` · C10 `py-fastapi-template` ·
S2 `plane` + `excalidraw` (`bulletproof-react` as the S1 no-fan-out contrast) · S3 `ef-api` + `ef-client` · E2 `golden-messy`.

---

## Tier-1 cells — full per-subsystem sign-off

The action/signal is the same shape across the three core languages; the file you
edit changes per cell. Per cell, pick a real source file of that language:
C1 → a `.tsx`/`.ts` under `excalidraw-app/` or `packages/`; C5 → an `app/**/*.rb`;
C7 → a `readthedocs/**/*.py`; E1 → an `app/**/*.rb` (large-repo timing focus).

| # | Subsystem | Do this (real session action) | Pass signal | Negative / off-state check |
|---|---|---|---|---|
| 1 | Hooks | Edit a source file (triggers PreToolUse); finish a turn (triggers Stop) | `<chameleon-context>` block appears before the edit; turn-end review runs; clean transcript, no error | `CHAMELEON_DISABLE=1` → no injection at all |
| 2 | Skills | Run `/chameleon-status`; then ask a normal coding question | status skill renders profile/trust/drift; the skill does NOT fire on the unrelated question | a non-chameleon request must not trigger a `/chameleon-*` skill |
| 3 | MCP tools | Ask Claude "where is X defined / what calls Y" so it calls `search_codebase`/`get_callers` | correct file:line results; valid envelope | a bogus symbol → clean "not found", no crash |
| 4 | Statusline | Open the repo; trust it; refresh | statusline shows `repo (trusted)`, updates after refresh; no garbling | `CHAMELEON_DISABLE=1` → statusline blank |
| 5 | Daemon | Work a session; `kill` the daemon pid; edit again | hook re-spawns the daemon, edit still advises; two repos open at once stay isolated | stop the daemon → it self-exits on idle, no orphan |
| 6 | Merge driver | Register the driver (two commands — see "Merge-driver registration" below); create a real `.chameleon/idioms.md` conflict; `git merge` | clean union, zero conflict markers | unregistered → git shows normal markers (no silent corruption) |
| 7 | Migrations | (covered by load-path) open repo whose profile predates current schema | loads or re-derives cleanly; no crash | corrupt one artifact → fail-open + `/chameleon-refresh` repairs |
| 8 | Generated artifacts | `/chameleon-refresh`; inspect `.chameleon/*.json` | artifacts regenerate, valid JSON, consistent generation | delete an artifact → refresh rebuilds it |
| 9 | Data-dir state | Open two repos; close and reopen one | per-repo state isolated; persists across reopen | second repo's profile never leaks into the first |
| 10 | AST extractor | Edit a file with odd-but-valid syntax for the language | archetype still resolves; no crash | a syntactically broken file → fail-open, batch survives |
| 11 | Cross-cutting engines | Ask for blast radius of a widely-used function (esp. E1 gitlabhq) | transitive callers returned; correct at repo scale | a leaf function → empty, no false edges |
| 12 | Plugin packaging | Clean install from the packaged artifact on a fresh machine (`docs/install.md`); run a full real session; then uninstall | install works end to end; hooks auto-register via `hooks.json`; daemon auto-spawns from session-start; uninstall leaves nothing behind | the merge driver must NOT auto-register (manual — see #6) |
| FW | Framework awareness | C5 add a controller w/o route; C7 add a model w/o migration | turn-end co-change advisory fires | a complete change-set → no nag |
| 13 | Config + kill switches | Toggle each `CHAMELEON_*` switch (full list in `CLAUDE.md` "Environment variables" / goal item 13) | feature present ON, gone OFF; env overrides config | default state (nothing set) = intended |
| 14 | Version sync + build | `scripts/setup.sh --check`; check manifest versions | all prerequisites OK; 6 manifests aligned | — |
| 15 | Hot path | Edit on the heaviest cell (E1 gitlabhq); watch responsiveness | no perceptible stall; well under the 3s hook cap | — |

### Merge-driver registration (manual, per repo)

Copy the plugin's repo-root `.gitattributes-template` into the golden repo's
`.gitattributes` (or merge it into an existing one), then register the driver
once in that repo (or globally with `--global`):

```bash
git config merge.chameleon.name "chameleon profile merge"
git config merge.chameleon.driver "<plugin-dir>/scripts/chameleon-merge-driver.sh %O %A %B %P"
```

`<plugin-dir>` is the installed plugin root, e.g.
`~/.claude/plugins/cache/chameleon/chameleon/<version>`. Install does NOT do this
for you and `docs/install.md` does not cover it — registration is deliberately
manual and per-repo (see G-004 in `docs/gap-log.md`); the template's own comment
block carries the same two commands.

## Tier-2 cells — spot-check (language-varying subsystems)

For each: open the repo, edit one source file, confirm the archetype resolves and the
framework is detected, then the cell's signature framework behavior:

- **C2 `golden-ts-nextjs`** — `detect_repo` shows `framework: nextjs`; a `route.ts` edit
  resolves the `app-route-handler` archetype.
- **C3 `golden-ts-nestjs`** — add a new `*.controller.ts` with no `*.module.ts` in the
  change-set → turn-end advisory "controller not registered in a @Module" fires; add the
  module too → no advisory.
- **C4 `ef-api`** — edit an `app/services/**/*.rb` → the `service` archetype resolves
  with its class contract (base `ActiveInteraction::Base`, required `execute`); ask
  "what calls <SomeService>" → the Ruby constant graph returns real cross-file callers;
  a bogus constant → clean not-found. (ef-api bootstraps `framework=rails`; this cell
  spot-checks the Ruby language pipeline on framework-neutral subsystems — the Rails
  layer itself is C5's job.)
- **C6 `golden-py-plain`** — ask "what calls `Record`" → cross-file callers returned
  (this is the src-layout fix; was empty before v2.38.5).
- **C8 `py-django-readthedocs` (DRF subset)** — the authz-guard layer is data-gated:
  `required_guards.authz_required` derives only when ≥60% of a view cohort makes an
  authz decision. On readthedocs it correctly derives NOTHING (public doc views
  dominate), so a new unguarded view must draw NO authz advisory — the pass signal is
  the absence of a false positive. To watch it fire, use `plane` (its Django apiserver
  derives `view: authz_required` at 0.6 frequency): a new view file declaring a class
  with no `permission_classes` / authz decorator / authz base → info advisory "AUTHZ:
  views in this archetype usually restrict access…"; add `permission_classes` → silent.
- **C9 `py-flask-flaskbb`** / **C10 `py-fastapi-template`** — `framework` detected
  (flask/fastapi); cross-file `get_callers` returns real edges (C10 exercises the
  `backend/` source-root fix).
- **S2 `plane` / `excalidraw`** — in `plane` (pnpm workspace: 20 workspace
  entries, 18 member profiles — two members are non-code config packages with
  status `failed_unsupported_language`) edit a file under `packages/*` → the
  per-edit advisory resolves against
  that member's OWN profile (its `workspace.parent` points at the root); finish a turn
  from the repo root → the multi-root Stop backstop still gates the member edits
  (see also the pure-coordinator-root note in `docs/verification-matrix.md` § A).
  `excalidraw` is the yarn-workspaces variant of the same fan-out (nested member
  profile under `excalidraw-app/`). As a contrast shape, `bulletproof-react` (S1)
  has `apps/*` dirs and recorded `workspace_roots` but `is_workspace: false` and
  ONE root profile — confirm it does NOT fan out. Negative: a member edit must not
  surface another member's conventions.
- **S3 `ef-api` + `ef-client`** — work both repos in one sitting: a Ruby edit in
  `ef-api` draws Ruby/Rails context, a TS edit in `ef-client` draws TypeScript
  context; the statusline tracks each repo's own trust state. Negative: neither
  repo's archetypes or idioms may leak into the other's advisories.
- **E2 `golden-messy`** — edit the conflict-markered `src/version.ts` and a unicode-named
  file → no crash (per-file isolation); detection picks the dominant language.

---

## What this runbook does not replace

The sign-off itself. Reading this and running it is the human verification the goal
requires; an agent cannot perform it (the goal, line 28-29). One residue is fully
human: a clean install on a literal fresh physical machine (the autonomous clean-room
install simulation is in `docs/gap-log.md`; the real-machine + real-session run is yours).
