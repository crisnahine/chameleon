# Verification runbook — turnkey human sign-off

> Item 1 of the Definition of Done (`docs/chameleon-goal.md`) is the one gate no
> automation can close: a human driving each golden repo through a real Claude Code
> session and recording the result. This runbook makes that gate mechanical — per
> cell, the exact action to perform, the pass signal to look for, and the negative
> (off-state) check. Record each result in `docs/verification-matrix.md`.
>
> Everything below has been pre-verified by automated scaffolding (it found and fixed
> 3 real defects, see `docs/gap-log.md`), so you are confirming, not debugging. If any
> step does NOT show the pass signal, that opens a gap — log it.

## How to use

1. Open the cell's golden repo in a real Claude Code session with chameleon installed.
2. Run the action. Watch the transcript / files / terminal for the pass signal.
3. Run the negative check (kill switch off, or a should-NOT-fire request).
4. Mark the (subsystem × cell) cell `PASS` or `FAIL` in `docs/verification-matrix.md`.

Golden repos (all under `~/Documents/Projects/Testing Apps/`, all bootstrapped):
C1 `excalidraw` · C5 `forem` · C7 `py-django-readthedocs` · E1 `gitlabhq` ·
C2 `golden-ts-nextjs` · C3 `golden-ts-nestjs` · C6 `golden-py-plain` ·
C9 `py-flask-flaskbb` · C10 `py-fastapi-template` · E2 `golden-messy`.

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
| 6 | Merge driver | Register per `docs/install.md`; create a real `.chameleon/idioms.md` conflict; `git merge` | clean union, zero conflict markers | unregistered → git shows normal markers (no silent corruption) |
| 7 | Migrations | (covered by load-path) open repo whose profile predates current schema | loads or re-derives cleanly; no crash | corrupt one artifact → fail-open + `/chameleon-refresh` repairs |
| 8 | Generated artifacts | `/chameleon-refresh`; inspect `.chameleon/*.json` | artifacts regenerate, valid JSON, consistent generation | delete an artifact → refresh rebuilds it |
| 9 | Data-dir state | Open two repos; close and reopen one | per-repo state isolated; persists across reopen | second repo's profile never leaks into the first |
| 10 | AST extractor | Edit a file with odd-but-valid syntax for the language | archetype still resolves; no crash | a syntactically broken file → fail-open, batch survives |
| 11 | Cross-cutting engines | Ask for blast radius of a widely-used function (esp. E1 gitlabhq) | transitive callers returned; correct at repo scale | a leaf function → empty, no false edges |
| 12 | Framework awareness | C5 add a controller w/o route; C7 add a model w/o migration | turn-end co-change advisory fires | a complete change-set → no nag |
| 13 | Config + kill switches | Toggle each `CHAMELEON_*` switch (full list in `CLAUDE.md` "Environment variables" / goal item 13) | feature present ON, gone OFF; env overrides config | default state (nothing set) = intended |
| 14 | Version sync + build | `scripts/setup.sh --check`; check manifest versions | all prerequisites OK; 6 manifests aligned | — |
| 15 | Hot path | Edit on the heaviest cell (E1 gitlabhq); watch responsiveness | no perceptible stall; well under the 3s hook cap | — |

## Tier-2 cells — spot-check (language-varying subsystems)

For each: open the repo, edit one source file, confirm the archetype resolves and the
framework is detected, then the cell's signature framework behavior:

- **C2 `golden-ts-nextjs`** — `detect_repo` shows `framework: nextjs`; a `route.ts` edit
  resolves the `app-route-handler` archetype.
- **C3 `golden-ts-nestjs`** — add a new `*.controller.ts` with no `*.module.ts` in the
  change-set → turn-end advisory "controller not registered in a @Module" fires; add the
  module too → no advisory.
- **C6 `golden-py-plain`** — ask "what calls `Record`" → cross-file callers returned
  (this is the src-layout fix; was empty before v2.38.5).
- **C9 `py-flask-flaskbb`** / **C10 `py-fastapi-template`** — `framework` detected
  (flask/fastapi); cross-file `get_callers` returns real edges (C10 exercises the
  `backend/` source-root fix).
- **E2 `golden-messy`** — edit the conflict-markered `src/version.ts` and a unicode-named
  file → no crash (per-file isolation); detection picks the dominant language.

---

## What this runbook does not replace

The sign-off itself. Reading this and running it is the human verification the goal
requires; an agent cannot perform it (the goal, line 28-29). One residue is fully
human: a clean install on a literal fresh physical machine (the autonomous clean-room
install simulation is in `docs/gap-log.md`; the real-machine + real-session run is yours).
