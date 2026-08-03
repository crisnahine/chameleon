# Verification Matrix — subsystem × cell sign-off tracker

> This file is the source of truth for "all" in the Chameleon correctness goal
> (`docs/chameleon-goal.md`). It is a **sign-off tracker**: a Tier-1 cell is "done"
> only when a person has driven the golden repo for that cell through a real Claude
> Code session and recorded a pass via the Verification Protocol; a Tier-2 cell may
> be closed by automation (`PASS-AUTO`) per the 2026-07-27 goal amendment. It is distinct
> from `docs/language-support-matrix.md`, which is a per-dimension capability-parity
> reference and is an *input* to this tracker.
>
> **No automated test result closes a TIER-1 cell.** Linters and `bench_hot_path.py`
> are developer scaffolding everywhere — they find bugs faster and earn zero "done"
> credit. Since the 2026-07-27 goal amendment, the `qa_*.py` batteries and the
> journey harness MAY close a Tier-2 cell, recorded as `PASS-AUTO` with the run
> artifact. Tier-1 (C1, C5, C7, E1) and subsystem #12 remain human-only: those
> sign-offs start `PENDING` and only a human flips them.

Status legend: `PENDING` (not yet verified) · `PASS` (human-signed, incl. the
negative/off-state check) · `PASS-AUTO` (Tier-2 only: closed by the journey harness
or a `qa_*.py` battery, run artifact recorded) · `FAIL` (opens a gap in
`docs/gap-log.md`) · `N/A` (subsystem does not apply to this cell).

---

## A. The cell grid (derived from code, not memory)

The cell grid covers the FIRST-CLASS languages, and that scoping is deliberate
(`docs/chameleon-goal.md` § Task 0). Verified in code:

- The first-class three are declared in `chameleon_mcp/language_support.py`
  (`_CAPABILITIES` seeds `typescript`, `ruby`, `python` via `_first_class`), and they
  are the only ones `detect_language()` classifies, the gate every per-edit lint rule
  reads (`plugin/mcp/chameleon_mcp/lint_engine.py`, `detect_language`).
- Extensions: TS/JS `.ts .tsx .js .jsx .mjs .cjs`; Ruby `.rb`; Python `.py .pyi`
  (`lint_engine.py`, `_TS_EXTENSIONS` / `_RUBY_EXTENSIONS` / `_PY_EXTENSIONS`).
- Go, Rust, Java, C# and PHP ARE supported, at the extraction tier: five declarative
  specs in `extractors/treesitter/lang/specs.py` (`ALL`), spliced into `EXTRACTORS`
  by `_spec_driven_extractor_classes()` in `extractors/registry.py`. They derive a
  profile (archetypes, canonical witness, signatures, imports) and get secret and
  eval-sink detection; they get no per-edit lint rules, no reverse index and no graded
  cross-file edges. They carry **no cell here**: the tier ships covered by unit tests
  (`tests/unit/test_spec_driven_lang.py`, `tests/unit/test_language_support.py`), with
  no golden repo and no human sign-off. That is the recorded scope, not an oversight:
  a cell added later needs a golden repo first.

The framework-aware families, the ones that earn a deeper layer and therefore a cell,
are the six the hardcoded arms of `_classify_framework` return
(`plugin/mcp/chameleon_mcp/bootstrap/orchestrator.py`): `rails`, `django`, `flask`,
`fastapi`, `nextjs`, `nestjs`. DRF is **not** a separate tag: it is recognized as
Django-family plus the dedicated DRF/Django authz-guard layer, so it is a sub-cell of
Django. The stored `framework` tag itself is wider: when no arm matches,
`_classify_framework` falls through to `_taxonomy_framework`, which scores the 64
framework profiles in `chameleon_mcp/knowledge/taxonomy.json` and can return any of
them. Those tags are descriptive metadata with no behavior attached, so they open no
cell either.

| # | Cell (language × framework) | Tier | Golden repo | Profiled |
|---|---|:--:|---|:--:|
| C1 | TypeScript/JS — agnostic | **1** | `excalidraw` | yes |
| C2 | TypeScript/JS — Next.js | 2 | `golden-ts-nextjs` (built; framework=nextjs, app-route-handler role verified) | yes |
| C3 | TypeScript/JS — NestJS | 2 | `golden-ts-nestjs` (built; co-change advisory verified) | yes |
| C4 | Ruby — agnostic | 2 | `ef-api` | yes |
| C5 | Ruby — Rails | **1** | `forem` (also `gitlabhq`, `mastodon`, `maybe`) | yes |
| C6 | Python — agnostic (plain scripts) | 2 | `golden-py-plain` (built) | yes |
| C7 | Python — Django | **1** | `py-django-readthedocs` | yes |
| C8 | Python — Django + DRF (authz-guard) | 2 | `py-django-readthedocs` (DRF subset) | yes |
| C9 | Python — Flask | 2 | `py-flask-flaskbb` | yes |
| C10 | Python — FastAPI | 2 | `py-fastapi-template` | yes |

Repo **shapes** (orthogonal to framework, all handled agnostically):

| # | Shape | Exercised on | Tier |
|---|---|---|:--:|
| S1 | single-package | `py-django-readthedocs`, `bulletproof-react` | 1 (readthedocs folds into C7; `bulletproof-react` is the S2 no-fan-out contrast: `apps/*` dirs, recorded `workspace_roots`, but `is_workspace: false` and one root profile) |
| S2 | monorepo / workspace (`packages`/`apps`/`libs`/`workspaces`) | `plane`, `excalidraw` | 2 |
| S3 | hybrid frontend+backend | `ef-api` (Ruby) + `ef-client` (TS) | 2 |

Edge / robustness:

| # | Repo | Purpose | Tier |
|---|---|---|:--:|
| E1 | `gitlabhq` | large/real Rails repo (size, cross-file at scale) | 1 (size check) |
| E2 | `golden-messy` (built) | polyglot, odd-but-legal syntax, stale data-dir state, in-progress merge. Since polyglot derivation landed this cell is no longer crash-resilience alone: bootstrap clusters every secondary language the repo genuinely contains, so the pass signal now includes per-language archetypes plus a primary that did not move (see the runbook) | 2 |

Dimension notes (scoping):

- **Windows** is a CI-verified dimension, not a sign-off column. Native Windows
  support (the `plugin/hooks/run-hook.cmd` polyglot launcher, `msvcrt`-based locking in
  `plugin/mcp/chameleon_mcp/locks.py`) is exercised by the CI matrix: the `test-windows`
  job (import smoke + cross-platform locking) and the `runtime-windows` job, which
  drives `run-hook.cmd` → Git Bash → venv python for the five fast hooks plus a
  real bootstrap → trust → refresh (`tests/ci_windows_runtime.py`; the sixth
  hook, the Stop backstop, is not driven there — its coverage is the unit
  suite on the POSIX matrix). Human per-cell
  verification in this tracker happens on the primary (POSIX) platform.
- **Monorepo pure-coordinator root** is part of S2's checklist, not a separate
  cell: a session launched at a workspace root that itself derives no profile
  (bootstrap status `success_workspaces_only`,
  `plugin/mcp/chameleon_mcp/bootstrap/orchestrator.py`) must still gate member-file edits
  at turn end via the multi-root Stop backstop (`_discover_stop_roots` in
  `plugin/mcp/chameleon_mcp/hook_helper.py`; kill switch `CHAMELEON_MULTIROOT_STOP=0`).
  Drive S2 both from inside a member workspace and from the coordinator root; the
  `qa-coord-shared` / `qa-coord-local` fixtures exercise the profile-less-root case.

### Golden-repo gaps (now closed at asset level — see `docs/gap-log.md`)

- **G-001 (FIX-STAGED)** — `golden-ts-nestjs` built and bootstrapped
  (`framework=nestjs`, 8 feature modules + guards). The controller→module co-change
  advisory is verified to fire/suppress/gate. C3 is now drivable.
- **G-002 (FIX-STAGED)** — `golden-py-plain` built and bootstrapped
  (`language=python`, `framework=None`, a real `datakit` library/CLI). C6 is now
  drivable.
- **G-003 (FIX-STAGED)** — `golden-messy` built and bootstrapped (polyglot,
  in-progress merge, stale `.tmp`); resilience verified (no crash, dominant-language
  detection, per-file isolation). E2 is now drivable.

These assets unblock the cells for verification; building+bootstrapping them is
scaffolding (zero done-credit). A Tier-1 sign-off stays `PENDING` until a HUMAN drives
it; a Tier-2 cell may be closed `PASS-AUTO` by the journey harness or a `qa_*.py`
battery, per the 2026-07-27 amendment.

---

## B. Tiering rationale

- **Tier 1** = fully human-verified for every relevant subsystem. Chosen as the
  deepest, most-exercised cell per language, each with a mature golden repo:
  **C1 (TS-agnostic), C5 (Ruby-Rails), C7 (Python-Django)**, plus **E1** for size.
- **Tier 2** = spot-check (human OR automated, per the 2026-07-27 amendment) on the
  subsystems most likely to vary by language
  (the language pipeline, generated artifacts, cross-cutting engines, enforcement):
  C2, C3, C4, C6, C8, C9, C10, S2, S3, E2.

This keeps hand-verification finite while covering every language and every
framework-aware family at least at spot-check depth, per the goal's philosophy
(Tier-1 always human; Tier-2 spot-check, automatable since the 2026-07-27 amendment).

---

## C. Subsystem applicability per tier

All 15 subsystems are required at Tier 1 (#12 packaging is machine-scoped — see
below). Tier-2 cells require the language-varying subsystems (bold) plus any
subsystem whose behavior the cell is the unique witness for (e.g. C3 → #2/#3/#11
NestJS pairing; C8 → #11 authz-guard).

1. Hooks · 2. Skills · 3. MCP tools · 4. Statusline · 5. Daemon · 6. Merge driver ·
7. Migrations · 8. **Generated artifacts** · 9. Data-dir state · 10. **AST
dumpers/extractors** · 11. **Cross-cutting engines** · 12. Plugin packaging ·
13. Config + kill switches · 14. Version sync + build/CI · 15. Hot-path budget.

Numbering follows `docs/chameleon-goal.md` § "The 15 subsystems" — #12 there is
**Plugin packaging** (an earlier revision of this matrix listed "Framework
awareness" as #12 and tracked packaging nowhere; both are tracked now).
**FW. Framework awareness** is a matrix-local extra row, not one of the goal
doc's 15: the goal treats framework behavior as part of the cell grid itself
(the cells ARE language × framework), and this tracker gives it an explicit
sign-off row so per-cell framework behavior cannot fall through the cracks.

Subsystems #4 (statusline), #5 (daemon), #6 (merge driver), #7 (migrations),
#14-15 are largely language-independent — verify once on a Tier-1 cell, spot-check
elsewhere only if a cell-specific risk is identified. #12 (plugin packaging) is
machine-scoped, not cell-scoped: one clean install + full real session on a fresh
machine signs it off (tracked under C1 in the Tier-1 table; the clean-room install
simulation in `docs/gap-log.md` is scaffolding, zero credit). FW (framework
awareness) is language- AND framework-varying — it is a required spot-check on
every Tier-2 cell. #13 (config + kill switches) gets its full switch-surface sweep
once, on a Tier-1 cell; the #13 row on every Tier-2 cell records a scoped
spot-check of just the switches/config that gate that cell's signature behavior
(e.g. the co-change/enforcement toggles for C3, the authz-guard advisory path for
C8) — a cell-specific feature's off-state can only be proven on the cell that
exhibits it, which is why the tracker table carries #13 on all ten Tier-2 cells.

---

## D. Sign-off tracker

Every Tier-1 cell below is `PENDING` until a human runs the Verification Protocol
(`docs/chameleon-goal.md` § Verification protocol) and records the result here,
including the step-4 negative/off-state check. A Tier-2 cell may instead be recorded
`PASS-AUTO` with the run artifact that closed it. **Turnkey per-cell steps (action →
pass signal → negative check) are in `docs/verification-runbook.md`** — run those and
mark each cell. Automated scaffolding has been run as a bug-finder (see
`docs/gap-log.md`); it does not populate this table.

### Tier 1 (full per-subsystem human verification)

| Subsystem | C1 TS-agnostic | C5 Ruby-Rails | C7 Py-Django | E1 large |
|---|:--:|:--:|:--:|:--:|
| 1. Hooks | PENDING | PENDING | PENDING | PENDING |
| 2. Skills | PENDING | PENDING | PENDING | PENDING |
| 3. MCP tools | PENDING | PENDING | PENDING | PENDING |
| 4. Statusline | PENDING | PENDING | PENDING | PENDING |
| 5. Daemon | PENDING | PENDING | PENDING | PENDING |
| 6. Merge driver | PENDING | PENDING | PENDING | PENDING |
| 7. Migrations | PENDING | PENDING | PENDING | PENDING |
| 8. Generated artifacts | PENDING | PENDING | PENDING | PENDING |
| 9. Data-dir state | PENDING | PENDING | PENDING | PENDING |
| 10. AST dumpers/extractors | PENDING | PENDING | PENDING | PENDING |
| 11. Cross-cutting engines | PENDING | PENDING | PENDING | PENDING |
| 12. Plugin packaging | PENDING (once — fresh machine) | N/A | N/A | N/A |
| FW. Framework awareness | PENDING | PENDING | PENDING | PENDING |
| 13. Config + kill switches | PENDING | PENDING | PENDING | PENDING |
| 14. Version sync + build/CI | PENDING | PENDING | PENDING | PENDING |
| 15. Hot-path budget | PENDING | PENDING | PENDING | PENDING |

### Tier 2 (human spot-check on language-varying subsystems)

| Subsystem | C2 Next | C3 Nest | C4 Ruby | C6 Py | C8 DRF | C9 Flask | C10 FastAPI | S2 mono | S3 hybrid | E2 messy |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| 8. Generated artifacts | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| 10. AST dumpers/extractors | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| 11. Cross-cutting engines | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| FW. Framework awareness | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| 13. Config + kill switches | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |

All cells are now drivable (the three previously-blocking golden repos are built and
bootstrapped).

**Sign-off authority differs by tier since the 2026-07-27 goal amendment.** A
Tier-1 cell (C1, C5, C7, E1) and subsystem #12 are `PENDING` until a HUMAN drives
them through the Verification Protocol. A Tier-2 cell may be closed by automation
— the journey harness plus the `qa_*.py` batteries — recorded with the run
artifact that closed it, using status `PASS-AUTO` so the grid never conflates the
two grades of evidence. See "Amendment 2026-07-27" in `docs/chameleon-goal.md` for
the reasoning and the accepted cost.

---

## E. Honesty note

This tracker reflects reality on the date it was generated:

- The cell grid and the framework family list are derived from code
  (`language_support.py`, `lint_engine.py`, `extractors/registry.py`,
  `bootstrap/orchestrator.py`), not from memory.
- The grid covers the first-class tier only. The five extraction-tier languages ship
  with unit-test coverage and no cell, which means no human has driven them through a
  real session; read the empty space as unverified, not as verified-absent.
- No cell is marked `PASS`. Per the goal, only a human running a real session may do
  that, and that has not happened yet. Since the 2026-07-27 amendment a Tier-2 cell
  may instead be marked `PASS-AUTO` by the journey harness or a `qa_*.py` battery;
  no cell carries that yet either, because taking the relaxation and running the
  campaign are two separate acts and only the first has happened.
- The three golden-repo gaps (G-001 NestJS, G-002 Python plain, G-003 messy repo) are
  now closed at the asset level — the repos are built and bootstrapped — so every cell
  is drivable. They remain `FIX-STAGED` in `docs/gap-log.md` (asset created; human
  sign-off still pending). G-006 (NestJS cluster naming) was investigated, grounded
  by experiment (`golden-ts-nestjs-rolegrouped`), and closed WONT-FIX in
  `docs/gap-log.md` — works as designed, not a bug.
- Automated scaffolding results live in `docs/gap-log.md` as bug-finder output, never
  as sign-off evidence here.
