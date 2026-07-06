# Verification Matrix — subsystem × cell sign-off tracker

> This file is the source of truth for "all" in the Chameleon correctness goal
> (`docs/chameleon-goal.md`). It is a **human sign-off tracker**: a cell is "done"
> only when a person has driven the golden repo for that cell through a real Claude
> Code session and recorded a pass via the Verification Protocol. It is distinct
> from `docs/language-support-matrix.md`, which is a per-dimension capability-parity
> reference and is an *input* to this tracker.
>
> **No automated test result closes a cell.** Linters, the `qa_*.py` batteries, the
> journey harness, and `bench_hot_path.py` are developer scaffolding — they find bugs
> faster, but they earn zero "done" credit. Every sign-off below starts `PENDING` and
> only a human flips it.

Status legend: `PENDING` (not yet human-verified) · `PASS` (human-signed, incl. the
negative/off-state check) · `FAIL` (opens a gap in `docs/gap-log.md`) · `N/A`
(subsystem does not apply to this cell).

---

## A. The cell grid (derived from code, not memory)

The supported-language set is closed at three, verified in code:

- `detect_language()` returns only `typescript` / `ruby` / `python` / `None`
  (`mcp/chameleon_mcp/lint_engine.py:158`).
- `EXTRACTORS = [TypeScriptExtractor, RubyExtractor, PythonExtractor]`
  (`mcp/chameleon_mcp/extractors/registry.py:26`).
- Extensions: TS/JS `.ts .tsx .js .jsx .mjs .cjs`; Ruby `.rb`; Python `.py .pyi`
  (`lint_engine.py:78-80`). No Go/Rust/Java/C# extractor, dumper, or detection
  signal exists — they MUST NOT appear here.

The framework-aware families are the discrete returns of `_classify_framework`
(`mcp/chameleon_mcp/bootstrap/orchestrator.py`): `rails`, `django`, `flask`,
`fastapi`, `nextjs`, `nestjs`, else `None` (agnostic). DRF is **not** a separate
tag — it is recognized as Django-family plus the dedicated DRF/Django authz-guard
layer, so it is a sub-cell of Django.

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
| E2 | `golden-messy` (built) | polyglot, odd-but-legal syntax, stale data-dir state, in-progress merge | 2 |

Dimension notes (scoping):

- **Windows** is a CI-verified dimension, not a sign-off column. Native Windows
  support (the `hooks/run-hook.cmd` polyglot launcher, `msvcrt`-based locking in
  `mcp/chameleon_mcp/locks.py`) is exercised by the CI matrix: the `test-windows`
  job (import smoke + cross-platform locking) and the `runtime-windows` job, which
  drives `run-hook.cmd` → Git Bash → venv python for the five fast hooks plus a
  real bootstrap → trust → refresh (`tests/ci_windows_runtime.py`; the sixth
  hook, the Stop backstop, is not driven there — its coverage is the unit
  suite on the POSIX matrix). Human per-cell
  verification in this tracker happens on the primary (POSIX) platform.
- **Monorepo pure-coordinator root** is part of S2's checklist, not a separate
  cell: a session launched at a workspace root that itself derives no profile
  (bootstrap status `success_workspaces_only`,
  `mcp/chameleon_mcp/bootstrap/orchestrator.py`) must still gate member-file edits
  at turn end via the multi-root Stop backstop (`_discover_stop_roots` in
  `mcp/chameleon_mcp/hook_helper.py`; kill switch `CHAMELEON_MULTIROOT_STOP=0`).
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

These assets unblock the cells for human verification; building+bootstrapping them is
scaffolding (zero done-credit). The sign-offs below stay `PENDING` until a human
drives them.

---

## B. Tiering rationale

- **Tier 1** = fully human-verified for every relevant subsystem. Chosen as the
  deepest, most-exercised cell per language, each with a mature golden repo:
  **C1 (TS-agnostic), C5 (Ruby-Rails), C7 (Python-Django)**, plus **E1** for size.
- **Tier 2** = human spot-check on the subsystems most likely to vary by language
  (the language pipeline, generated artifacts, cross-cutting engines, enforcement):
  C2, C3, C4, C6, C8, C9, C10, S2, S3, E2.

This keeps hand-verification finite while covering every language and every
framework-aware family at least at spot-check depth, per the goal's philosophy
(Tier-1 always human; Tier-2 human spot-check).

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

Every cell below is `PENDING` until a human runs the Verification Protocol
(`docs/chameleon-goal.md` § Verification protocol) and records the result here,
including the step-4 negative/off-state check. **Turnkey per-cell steps (action →
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
bootstrapped). Every cell is `PENDING` until a human drives it through the
Verification Protocol and records the result.

---

## E. Honesty note

This tracker reflects reality on the date it was generated:

- The cell grid and the framework family list are derived from code (`lint_engine.py`,
  `extractors/registry.py`, `bootstrap/orchestrator.py`) — not from memory.
- No cell is marked `PASS`. Per the goal, only a human running a real session may do
  that, and that has not happened yet.
- The three golden-repo gaps (G-001 NestJS, G-002 Python plain, G-003 messy repo) are
  now closed at the asset level — the repos are built and bootstrapped — so every cell
  is drivable. They remain `FIX-STAGED` in `docs/gap-log.md` (asset created; human
  sign-off still pending). G-006 (NestJS cluster naming) was investigated, grounded
  by experiment (`golden-ts-nestjs-rolegrouped`), and closed WONT-FIX in
  `docs/gap-log.md` — works as designed, not a bug.
- Automated scaffolding results live in `docs/gap-log.md` as bug-finder output, never
  as sign-off evidence here.
