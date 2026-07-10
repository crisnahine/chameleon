# Gap log

> Every gap found while driving the correctness goal (`docs/chameleon-goal.md`),
> with its resolution. Distinct from the roadmap (`docs/parity-progress.md`).
>
> A gap is **closed** only after the fix is re-verified by a human via the
> Verification Protocol on *every affected cell* — not just the one where it
> surfaced (a language-specific bug usually implies siblings). Until then it stays
> `OPEN` or `FIX-STAGED` (fix landed, human re-sign-off pending).

Entry format: `ID — title` · subsystem · cell(s) · category
(logic / workflow / missing-step / edge-case / inconsistency / bug / perf) · status ·
repro · resolution.

Status legend: `OPEN` · `FIX-STAGED` (code landed, awaiting human re-sign-off) ·
`CLOSED` (human re-signed) · `WONT-FIX` (accepted, with rationale).

---

## Addendum — 2026-07-10 (v3 QA campaign: MCP fold + plugin/ restructure)

A 15-agent QA matrix over the v3 tree (48→19 MCP fold, `plugin/` restructure)
surfaced 24 findings, all P2/P3, no P0/P1. Recorded here by status.

FIX-STAGED (code/skill landed this campaign, human re-sign-off pending):
- **G-021 — Python test files bucketed into production role archetypes** · signatures ·
  bug · py-fastapi/Django · `python_role_for_path('tests/api/routes/test_x.py')`→`'route'` ·
  gated on `_is_test_path(language=python)` before role lookup (commit 1cf94d5).
- **G-022 — Python subdir linter-config unread** · bootstrap tool-config · missing-step ·
  FastAPI `backend/pyproject.toml [tool.ruff]` · Python config discovery read only the
  repo root; TS had the workspace-config subdir fallback, Python did not · mirrored the TS
  fallback (bounded, `WORKSPACE_FANOUT_CAP`, root-wins-outright) in `tool_config.py`
  (commit e624a25). Nuance: the original evidence (`rules_extracted=0` on the template)
  is only partly this gap — the template's own `[tool.ruff]` sets no `line-length`/
  `quote-style` (E501 ignored), so its count is unchanged; the parity fix is proven by a
  subdir config that declares a length now extracting.
- **G-023 — refresh skill re-lock call omitted `path`** · skills · bug · the documented
  `chameleon_lifecycle(action="bootstrap_repo")` could not bind · added `path` (d282204).
- **G-024 — receiving-code-review had no argument-hint; pr-review claimed a tool grant
  restriction the harness can't enforce** · skills · inconsistency · hint added, reviewer
  directed in-prompt not to call the review/lifecycle dispatchers (d282204).

BY-DESIGN — product correct, but the QA expectation was wrong (test fixed):
- **Next.js singleton `app/layout.tsx` resolves archetype=None.** Grounded: the fixture has
  1 layout vs 5 pages, so the pages cluster and the lone layout has no siblings — one file
  cannot yield a convention (n=1), and a real multi-layout app clusters layouts fine.
  Matching it to a page archetype would inject a misleading witness, so the honest
  `match_quality=none, confidence=low` is correct. The defect was the QA battery asserting
  every file must resolve; fixed to forgive the honest-no-cluster signal (commit 165732d).

NOT A PLUGIN DEFECT — maintenance / measurement:
- **Stale committed test-fixture profiles (`calls_index` schema v1) fail cross-file tools.**
  The engine correctly rejects the old schema and `doctor`/`get_drift_status` say "run
  /chameleon-refresh". Action is a one-command bed refresh per repo, not a code change;
  does not affect the shipped plugin.
- **Bench single-shot cold 92ms.** Measurement artifact (first-call import overhead); the
  robust multi-cold p50 (27ms) meets the budget. Not a regression.

BY-DESIGN with a noted P3 hardening backlog:
- **Daemon slowloris 5s wedge.** A half-sent frame stalls the single-threaded accept loop
  up to 5s; concurrent calls return None and hooks fall back in-process (no user-visible
  break), self-recovering when the recv timeout reaps the connection. Fail-open by design
  (the daemon is a latency layer, never correctness). Hardening the accept loop
  (non-blocking accept / per-connection recv deadline) is a legit low-value P3, not a v3
  blocker — it needs a hostile local actor writing half-frames to your own daemon socket.

OPEN (real P3 hardening, deferred past v3 — none block the release):
- **query_symbol_importers accepts a bare symbol name silently** (`found:true, importers:[]`
  vs the `found:false` sibling tools return on non-path input) — contract-honesty gap.
- **Turn-end backstop misses a `Bash mv` write vector** (`_extract_bash_write_targets`
  covers `>`/`>>`/`tee`/`sed -i`; `mv` destinations are not recorded).
- **Stale-trust sessions get no credential advisory** at PreToolUse (deny needs `trusted`,
  the untrusted advisory needs `untrusted`; stale falls between — only under the opt-in
  `CHAMELEON_TRUST_REVALIDATE=1`).
- **Noop refresh leaves `.chameleon.backup-<txn>` debris** if it crashes mid-swap
  (orphan sweep runs only in the full bootstrap path).
- **Silent linter-config parse failure** (broken symlink / malformed TOML → `rules=0`
  with no warning field on the bootstrap result and no `doctor` check).
- **Catchall directory clusters can serve a trivial canonical** (a 26-char `__init__.py`
  docstring as the structural exemplar) — canonical-selection quality.
- **Downgraded engine silently rebuilds a newer-schema profile down** and reports success
  with no downgrade notice.
- **Linter-config distillate extracts only a fixed key set** (`line_length`, `quote_style`,
  `indent`), so a repo that configures ruff/black richly but sets none of those (the FastAPI
  template: only `select`/`ignore`) yields zero format conventions. Capturing the linter's
  own rule set verbatim as a rules section (as rubocop/eslint are) is a separate artifact
  contract, surfaced by G-022.

Completeness note: the QA matrix drove batteries + hostile depth but not the `/chameleon-*`
slash-command flows at runtime (that is the Wave-5 journey harness's job) — and the journey
fixtures are TS + Rails only, so **Python lifecycle/damage depth needs a dedicated pass
before v3 ships** (tracked as the campaign's pre-release Python-depth gate).

---

## Addendum — 2026-07-07 (post-campaign status snapshot)

Everything below this section is the v2.38.x verification-campaign record (newest
entry 2026-06-29, v2.38.5) and is left as written. Recorded here rather than by
editing history:

- **Every staged fix has shipped.** The FIX-STAGED entries — G-001..G-003 (asset
  builds) and G-007..G-019 (code fixes) — all landed in tagged releases and are
  present in current code through `v2.54.0`. Spot-verified against today's tree:
  G-007 `_python_source_roots` (`plugin/mcp/chameleon_mcp/symbol_index.py:336`); G-008
  read ceiling derived from the build edge cap (`max_read_bytes =
  threshold_int("CALLS_INDEX_MAX_TOTAL_EDGES") * 700`,
  `plugin/mcp/chameleon_mcp/calls_index.py:663`); G-010 non-idioms top-title guard in
  `looks_like_idioms_markdown` (`plugin/mcp/chameleon_mcp/idiom_coverage.py:1176`);
  G-013 `fanout_clipped` (`plugin/mcp/chameleon_mcp/blast_radius.py:45-63`). Statuses
  stay FIX-STAGED because formal CLOSED still requires human re-sign-off per the
  protocol above — shipping is not sign-off.
- **G-020 superseded.** Class/module name search shipped in v2.50.0: the
  symbol-signatures artifact now carries an additive `classes` section
  (`plugin/mcp/chameleon_mcp/symbol_signatures.py` — built at :178, `class_items()` at
  :214; a pre-v2.50 artifact simply has no `classes` key until the next refresh)
  and `search_codebase` returns `kind="class"` rows. The WONT-FIX rationale ("no
  committed artifact exposes class shapes in a searchable form") no longer holds;
  the entry's own re-open-as-a-feature clause was exercised.
- **Scope boundary.** Gaps found after v2.38.5 are recorded in `CHANGELOG.md`
  release entries (v2.39.0 through v2.54.0), not here. This log is a bounded
  snapshot of the v2.38.x campaign, not a live tracker of all known gaps.
- **Numbering note.** Some entries below use "subsystem 12" for framework
  awareness (G-001, G-006) and others for packaging (G-004). Per
  `docs/chameleon-goal.md`, #12 is plugin packaging;
  `docs/verification-matrix.md` now tracks framework awareness as its own
  unnumbered `FW` row. The entries are left as written.

---

## Open gaps

### G-001 — NestJS golden repo (asset created, human sign-off pending)

- **Subsystem(s):** 8, 10, 11, 12 (framework awareness for TS-NestJS)
- **Cell(s):** C3 (TypeScript/JS — NestJS)
- **Category:** missing-step (test asset)
- **Status:** FIX-STAGED (repo built + bootstrapped + advisory verified; C3 now
  drivable, awaiting human sign-off)
- **Repro:** The NestJS framework-aware layer (controller→module co-change at
  `cochange.py:489`, `*.controller.ts`/`*.module.ts`/`*.guard.ts` role priors in
  `_TS_PRIORS`, `naming.py:486,511-516`) shipped with a unit fixture but no real,
  driven golden repo.
- **Resolution (done):** Built `~/Documents/Projects/Testing Apps/golden-ts-nestjs`
  — 8 feature modules (users, auth, products, orders, comments, tags, categories,
  reviews), each a `*.controller.ts` + `*.service.ts` + `*.module.ts`, plus guards,
  `@nestjs/core` + `@nestjs/common` in package.json, real git history. Bootstraps
  `framework=nestjs`. Verified via scaffolding: the `cochange-nestjs-controller-module`
  advisory FIRES on a lone new controller, SUPPRESSES when the module companion is in
  the change-set, and is GATED OFF on a non-NestJS repo (excalidraw).
- **Sizing note:** the advisory needs ≥ `COCHANGE_MIN_TRIGGER_FILES` (default 8)
  committed controllers to arm (the repo-applicability gate, `cochange.py:654`); the
  first 4-module draft was correctly silent — that is by-design, not a bug. The repo
  was expanded to 8 controllers so the feature is exercisable.

### G-002 — Python plain-script golden repo (asset created, human sign-off pending)

- **Subsystem(s):** 8, 10, 11, 12, 13
- **Cell(s):** C6 (Python — agnostic, framework=None)
- **Category:** missing-step (test asset)
- **Status:** FIX-STAGED (repo built + bootstrapped; C6 now drivable)
- **Resolution (done):** Built `~/Documents/Projects/Testing Apps/golden-py-plain`
  — a pure-Python `datakit` library/CLI (models/readers/transforms + CLI, 18 .py
  files, `__all__` surfaces, tests), no web-framework dependency, real git history.
  Bootstraps `language=python framework=None`; `get_pattern_context` resolves an
  archetype at `ast` match-quality.

### G-003 — Deliberately-messy-but-valid golden repo (asset created, sign-off pending)

- **Subsystem(s):** 10, 11 (resilience), 7/9 (in-progress merge, pre-existing state)
- **Cell(s):** E2
- **Category:** missing-step (test asset)
- **Status:** FIX-STAGED (repo built + bootstrapped + resilience verified)
- **Resolution (done):** Built `~/Documents/Projects/Testing Apps/golden-messy`
  — polyglot (11 TS/TSX dominant + 1 Python + 1 Ruby), odd-but-legal syntax (unicode
  identifiers `café`/`日本語`, a 5000-char line, a comment-only file, an empty file),
  a LIVE in-progress merge (`MERGE_HEAD` + conflict-markered `src/version.ts`), and a
  stale `.chameleon/.tmp/abandoned-txn-123` with no COMMITTED sentinel. Verified via
  scaffolding: bootstrap succeeds, detection picks `typescript` (dominant) despite the
  polyglot tree, `get_pattern_context` on the conflict-markered and unicode files does
  not crash (per-file isolation), the stale `.tmp` is ignored (not promoted), and a
  real profile is written.

### G-006 — NestJS role-named clusters on feature-co-located layout (resolved: works as designed)

- **Subsystem(s):** 3 (naming), 12 (framework awareness)
- **Cell(s):** C3
- **Category:** open question → resolved (NOT a bug)
- **Status:** WONT-FIX (works as designed; grounded by experiment)
- **Repro:** On `golden-ts-nestjs` (feature-co-located —
  `src/users/{users.controller,users.service,users.module}.ts`), clustering buckets by
  directory first, so each feature dir is one mixed-role cluster (size 3). No filename
  suffix is a majority, so the `_TS_PRIORS` role priors
  (`.controller.ts` → "controller", `naming.py:511-516`) get generic `cluster-<hash>`
  names.
- **Grounding (experiment):** Built `golden-ts-nestjs-rolegrouped`
  (`src/controllers/*.controller.ts`, `src/services/*.service.ts`,
  `src/modules/*.module.ts`) and bootstrapped it. The priors fire correctly there —
  clusters are named `controller`, `service`, `module`. So the priors WORK; the
  difference is purely the layout.
- **Resolution (verdict):** Not a bug. On a role-grouped layout the priors name by
  role; on a feature-co-located layout the directory IS the meaningful unit and the
  cluster represents "the users feature," not "a controller" — naming it "controller"
  would be incorrect. The headline NestJS feature (the co-change advisory) is
  filename-based and works on both layouts. No code change; closing WONT-FIX with the
  layout rationale above.

---

## Process gaps (documented behaviors, candidate WONT-FIX)

### G-004 — Install does not auto-register the merge driver

- **Subsystem(s):** 6 (merge driver), 12 (packaging)
- **Cell(s):** all
- **Category:** workflow (by design)
- **Status:** WONT-FIX (intentional; documented manual registration verified working)
- **Repro:** `setup.sh` / `docs/install.md` do not touch `.gitattributes`. The merge
  driver is registered manually (copy `.gitattributes-template`, `git config
  merge.chameleon.*`). Per the goal #6/#12 this is expected behavior, not a defect.
- **Resolution (verified):** Drove the documented registration end-to-end — copied
  `.gitattributes-template` into a fresh repo, ran the two `git config
  merge.chameleon.*` commands, created a genuine `git merge` conflict on
  `.chameleon/idioms.md`, and the driver **auto-fired during the real merge**: exit 0,
  zero conflict markers, both branches' idioms unioned. Install-no-auto-register is
  intentional (the user opts in per-repo). Closing WONT-FIX; a human may still
  sign off subsystem #6 on a golden repo to convert this to a matrix PASS.

---

### G-005 — No real old-schema (<8) migration fixture

- **Subsystem(s):** 7 (migrations)
- **Cell(s):** all
- **Category:** missing-step (test asset)
- **Status:** WONT-FIX (documented model; load-path safety verified) — re-open only
  if a human wants a true per-version migration fixture
- **Repro:** Current schema is 8 (`profile/schema.py:19`), max supported 8
  (`profile/loader.py:30`). Every `*-oldschema-*` repo in the bed is actually at
  schema_version=8 (re-bootstrapped over time), so genuine cross-version migration
  is only testable by synthetically downgrading a profile.
- **Resolution:** The migration model is **forward-compatible-load +
  `/chameleon-refresh` regenerate**, not per-version transformation. The loader
  (`profile/loader.py:626`) rejects a profile NEWER than `MAX_SUPPORTED_SCHEMA_VERSION`
  ("upgrade chameleon-mcp") and loads an OLDER one as-is if it is still structurally
  consistent; a structural mismatch raises `ProfileLoadError`, the hooks fail open,
  and the user re-derives with `/chameleon-refresh`. There is therefore no per-version
  migration code to fix. Verified (scaffolding): a downgraded v5 profile loads
  cleanly, a v99 profile is rejected, neither crashes. Authoring a faithful
  schema_version<8 fixture requires knowing each version's exact structure; deferred
  to a human who wants that specific coverage.

---

## Expert-panel confirmed defects (real bugs, fixed)

Found by the 14-cell expert verification panel (one specialist agent per matrix
cell, each driving real chameleon behavior; every anomaly adversarially re-run by
an independent skeptic before counting). 12/14 cells were fully clean; these two
were confirmed real and are now fixed with regression tests.

### G-007 — Empty cross-file call graph on non-flat Python layouts (FIXED, generalized)

- **Subsystem(s):** 11 (cross-file engines), 10 (extractor wiring)
- **Cell(s):** C6 (`src/`-layout), C10 (`backend/`-layout) — any Python repo whose
  package is not at the repo root
- **Category:** logic gap
- **Status:** FIX-STAGED (fixed + regression tests + re-verified; human sign-off pending)
- **Repro (was):** `make_module_resolver` / `_python_module_base` resolved an absolute
  Python import `pkg.sub` only as `root/pkg/sub`. On a src-layout repo (package under
  `src/`) or a service-dir layout (FastAPI template's package under `backend/app/`,
  imports `from app.models import ...`), every absolute-import edge dropped, so
  `calls_index.json` / `reverse_index.json` built EMPTY — `get_callers` /
  `get_blast_radius` / `query_symbol_importers` / contract-break / cross-file-dup all had
  zero data, and per-edit nearby-signature ranking lost its call facts. Failed safe (no
  crash, no false positives) but silently zeroed the subsystem.
- **Fix:** `symbol_index.py` — `_python_source_roots(root)` returns `[root]`, the PyPA
  `src/` root (always probed; covers PEP 420 namespace src-layouts with no `__init__`),
  AND any other immediate child that is not itself a package but contains one (e.g.
  `backend/`). The Python resolver probes each source root for absolute imports, root
  first, so flat-layout and package-rooted repos are unchanged. Relative imports unchanged.
- **Generalization note:** the first fix handled only `src/`; the regression panel found
  C10 (FastAPI, `backend/app/`) had the identical empty-graph bug for a non-`src` root, so
  the fix was widened to discover any non-package source root.
- **Re-verified:** resolver maps `datakit.models.record → src/datakit/models/record.py`
  and `app.models.record → backend/app/models/record.py`; re-bootstrapped golden-py-plain
  (9 caller edges, was 0) and py-fastapi-template (156 import-grade edges, was 0) build
  populated indexes; `get_callers` returns real data. Flat-layout Django (909 targets /
  3313 edges) and Flask (1106 edges) are byte-identical after re-bootstrap — no regression.
  Regression tests: `TestPythonSrcLayout` (src + flat + `backend/` + package-at-root guard)
  in `tests/unit/test_calls_index.py`.

### G-008 — Large valid calls_index rejected by too-small read cap (FIXED)

- **Subsystem(s):** 11 (cross-file engines), 15 (scale)
- **Cell(s):** E1 (gitlabhq) and any repo whose calls_index exceeds 16MB
- **Category:** inconsistency (build cap vs read cap)
- **Status:** FIX-STAGED (fixed + regression tests + re-verified; human sign-off pending)
- **Repro (was):** the builder caps on EDGES (`CALLS_INDEX_MAX_TOTAL_EDGES`=200k) but the
  reader (`calls_index.py:449`) rejected any file > a hardcoded 16MB. gitlabhq's valid
  21.8MB / ~40k-edge index (555 bytes/edge) was rejected → `get_callers` /
  `get_callees` / `get_blast_radius` all returned `no-calls-index` despite a correct,
  committed index.
- **Fix:** `calls_index.py` — derive the read ceiling from the build edge cap
  (`CALLS_INDEX_MAX_TOTAL_EDGES * 700` bytes), so the two can never drift. This loader is
  tool-time + the Stop judge, never the per-edit hot path, so the larger read is safe; it
  still rejects a genuinely build-cap-exceeding file.
- **Re-verified:** gitlabhq `get_callers` returns 8 real callers, `get_blast_radius`
  returns transitive data. Regression tests: `TestLoadReadCap` in
  `tests/unit/test_calls_index.py`. Sibling read caps left as-is: `exports_index` (8MB) and
  `symbol_signatures` (16MB) are read on the per-edit hot path and bounded deliberately to
  protect the 3s budget; neither is breached on the largest tested repo.

### G-009 — C2 golden repo (plane) is no longer Next.js (FIXED)

- **Subsystem(s):** 12 (framework awareness)
- **Cell(s):** C2 (TypeScript — Next.js)
- **Category:** inconsistency (stale test asset)
- **Status:** FIX-STAGED (new golden repo built; human sign-off pending)
- **Repro:** the panel found `plane` migrated off Next.js to react-router 7 + vite (no
  `next` dep anywhere; `_classify_framework` returns None). The matrix mapped C2 to plane,
  so the Next.js framework-aware layer had no real driven repo.
- **Fix:** built `~/Documents/Projects/Testing Apps/golden-ts-nextjs` — a real app-router
  Next.js app (`next` dep, `next.config.mjs`, route pages, API route handlers, components,
  api-client lib), bootstrapped `framework=nextjs`; the `app-route-handler` role archetype
  resolves at `ast` quality. C2 remapped to it; plane stays as an agnostic TS monorepo (S2).

### G-010 — Merge driver silently corrupted an idiom-bearing profile.summary.md (FIXED)

- **Subsystem(s):** 6 (merge driver)
- **Cell(s):** any profile carrying a taught/auto idiom (~47/306 testing-app summaries)
- **Category:** logic gap (over-broad content classifier → silent corruption)
- **Status:** FIX-STAGED (fixed + regression tests + re-verified; human sign-off pending)
- **Found by:** the depth/robustness expert panel (6/7 dimensions clean); adversarially
  confirmed (reproduced on a real ef-api `profile.summary.md`).
- **Repro (was):** `looks_like_idioms_markdown` (`idiom_coverage.py`) returned True on any
  `(?m)^###\s+\S` match. A `profile.summary.md` lists idioms under a `## Idioms`
  subsection (`### slug` blocks), so on a merge conflict it was misrouted to
  `merge_idioms_markdown`, which rewrote the summary (title forced to `# idioms`, spurious
  `## active`/`## deprecated` injected, the real summary content demoted, one side's edit
  dropped) and returned `status=success` → shell driver exit 0 → git staged a mangled file
  as a resolved merge with no conflict marker. This violated the `.gitattributes-template`
  contract that the non-idioms companion files DECLINE (exit 1, OURS preserved, conflict
  flagged) — "never silent corruption." `principles.md` was safe only by luck (its template
  has no `### `); the root cause was the content classifier, not the filename.
- **Fix:** `idiom_coverage.py` — `looks_like_idioms_markdown` now returns False when the
  document has a top-level (`# `) title that is not an idioms title (does not contain
  "idiom"). A real idioms.md (`# idioms`, a `# Team Idioms`, or a header-less file of
  `### slug` blocks) still unions; a summary / principles / any other titled doc declines
  via the existing JSON-parse fallthrough.
- **Re-verified:** the repro now declines (exit 1, OURS title `# chameleon profile summary`
  preserved); idioms.md still unions (both sides present); principles.md still declines.
  Regression tests in `tests/unit/test_idiom_coverage_tools.py`
  (`TestLooksLikeIdiomsMarkdown` + `test_merge_profiles_declines_idiom_bearing_summary`).

## Skills + comprehension panel defects (4th expert panel)

A 7-area audit of the previously-untested surface — the 13 `/chameleon-*` skill flows
and the comprehension tools — found **10 confirmed defects** (pr-review area clean).
Nine are fixed with regression tests; one is an accepted scoped limitation.

| ID | Sev | Defect | Status |
|---|---|---|---|
| G-011 | fix | `bootstrap_repo` MCP wrapper dropped `production_ref` (init/refresh skills' branch answer lost) | FIXED (`server.py`) + test |
| G-012 | fix | `doctor` config check cwd-anchored → reports configured repo as unconfigured from a subdir | FIXED (`tools.py`, walk to root) + test |
| G-013 | fix | `get_blast_radius` dropped callers at the fanout cap but reported `truncated:false` | FIXED (`blast_radius.py`) + 2 tests |
| G-014 | fix | receiving-review Step 3 security grounding no-ops on a null archetype (lint_file early-return) | FIXED (SKILL.md placeholder, matches pr-review) |
| G-015 | nit | deprecated-idiom write left the `## deprecated` `_(none)_` placeholder | FIXED (both write paths) + test |
| G-016 | nit | `search_codebase` returned `found:true` on an empty query (docstring promised false) | FIXED (`tools.py`) + test |
| G-017 | nit | `doctor` SKILL.md omitted `hook_interpreter_deps` from the check list + error remediation | FIXED (SKILL.md) |
| G-018 | nit | statusline update badge dropped the apply instruction in the no-`jq` fallback | FIXED (`statusline.sh`) |
| G-019 | nit | `get_crossfile_context` docstring undersold its Ruby constant-graph fallback | FIXED (docstring) |
| G-020 | nit | `search_codebase` does not index class/type/interface/module names (callable-only) | WONT-FIX (scoped) |

Verification note: the report also claimed `doctor` emits an `index_db` check — it does
NOT (the 12 real check names were confirmed in code), so that part was a false report and
no `index_db` reference was added (anti-hallucination on the report itself).
[Correction, 2026-07-07: this note was itself the false claim. `doctor()` does emit an
`index_db` check — `{"name": "index_db", ...}` in `plugin/mcp/chameleon_mcp/tools.py`, present
since v2.6.0 — so the original panel report was right and this note's rebuttal was the
hallucination. Kept, struck-by-correction, as its own anti-hallucination lesson.]

### G-020 — class/type/interface/module names not searchable (accepted limitation)

- **Subsystem(s):** 3 (comprehension)
- **Status:** WONT-FIX (scoped) — re-open as a feature if class search is wanted
- **Detail:** `search_codebase` / `search_symbols` walk `symbol_signatures` (callables
  only); class/type/interface/module declarations live in `class_shapes`, which no
  committed artifact exposes in a searchable form. Making them searchable requires a new
  class-name index (build + loader + search integration) — a subsystem-scope change, not a
  contained fix. Per the goal's "if a fix requires a redesign, log it rather than expand
  scope silently," it is recorded here. Callables remain fully searchable; this is a
  degraded-recall NIT, not a break.

## Scaffolding bug-finder results (zero done-credit — informational only)

Run while driving the goal. Per the goal philosophy these earn NO sign-off credit;
they are recorded so the human verifier knows where to focus and that no obvious
regression blocks verification. Date: 2026-06-29, v2.38.4.

| Scaffolding check | Result |
|---|---|
| Unit tests (`tests/unit/`) | 4813 passed, 3 skipped |
| ruff check + format | clean (390 files) |
| `qa_typescript.py` (excalidraw) | 59/59 |
| `qa_ruby.py` (forem, Rails) | 66/66 |
| `qa_python.py` (django-readthedocs) | 20/20 |
| `qa_crosscutting.py` (TS+Ruby) | 15/15 (incl. cross-repo isolation, daemon alive) |
| `qa_hook_simulation.py` | Pre 6/6, Post 6/6, Combined 6/6 (86-394 ms) |
| Hooks fail-open (6 malformed payloads × preflight/verify) | all exit 0, valid JSON |
| Statusline | correct per-repo state (`excalidraw/forem (trusted)`), 56 ms < 100 ms, `CHAMELEON_DISABLE` blanks output |
| MCP stdio transport | 46 tools over real stdio |
| Merge driver — idioms.md 3-way | exit 0, clean slug union |
| Merge driver — archetypes.json 3-way | exit 0, union + cluster_size preference |
| Merge driver — COMMITTED decline | exit 1, ours preserved (not corrupted) |
| Version sync (6 manifests) | all at 2.38.4 |
| Schema load (old v5 / new v99) | v5 loads, v99 rejected, no crash |
| `bench_hot_path.py` (excalidraw) | cold p99 ~25 ms, warm <1 ms — ~100× under the 3 s ceiling |
| Hot path on heaviest cell (gitlabhq) | cold p99 124.5 ms, warm 30.7 ms — 24× under 3 s ceiling |
| Kill-switch polarity audit (10 default-ON + 7 opt-in) | all correct (`!= "0"` / `== "0"` gate; `DISABLE`/opt-in `== "1"`) |
| Merge driver via REAL `git merge` (documented registration) | auto-fired, clean union, 0 conflict markers |
| New golden repos bootstrap | nestjs=nestjs, py-plain=python/None, messy=typescript/None — all `profile_present` |
| Hot path on ALL Tier-1 cells | excalidraw p99 ~25ms · forem 80ms · readthedocs 24ms · gitlabhq 124ms — all ≤124ms vs 3000ms ceiling |
| NEARBY_SIGNATURES observable off-state | ON → section rendered (261 chars); `=0` → empty (feature fully suppressed) |
| G-006 grounding (role-grouped NestJS) | priors fire correctly → clusters named controller/service/module |
| Install tooling (`setup.sh --check`) | exit 0, all prerequisites OK (uv/node22/npm/ruby+prism/timeout) |
| Plugin manifests + hooks.json | valid JSON; 6 hook events registered (auto-registration source) |
| Expert panel (14 cells, v2.38.4) | 12/14 clean; 2 defects found+confirmed → G-007, G-008 |
| Regression panel (8 cells, v2.38.5) | both fixes confirmed; 0 regressions; surfaced the `backend/` generalization (folded into G-007) |
| Full unit suite after fixes | 4819 passed, 3 skipped; ruff + format clean |
| Clean-install simulation (subsystem #12, item 4) | fresh copy (no prebuilt deps) → `setup.sh` builds Python (uv) + Node (npm) deps from scratch; preflight hook fires from the installed location (exit 0, injects context); MCP server serves 46 tools; bootstrap succeeds; runtime state lands in an isolated data dir (real `~/.local/share/chameleon` untouched); uninstall (`rm`) leaves nothing behind. The literal fresh-physical-machine + full real Claude Code session remains the human part of item 4. |
| Depth/robustness panel (7 dims, v2.38.5) | 6/7 clean (daemon concurrency, migrations/old-schema, data-dir isolation, degraded-artifact, boundary inputs, all-6-hook fuzzing); 1 defect found+confirmed → G-010 (now fixed) |
| Full unit suite after G-010 | 4823 passed, 3 skipped; ruff + format clean |
| Skills/comprehension panel (7 areas, v2.38.5) | pr-review clean; 10 defects found → 9 fixed (G-011..G-019), 1 scoped WONT-FIX (G-020) |
| Full unit suite after all 12 fixes | 4829 passed, 3 skipped; ruff + format clean |
| Final integration pass on post-fix code | qa_typescript 59/59, qa_ruby 66/66, qa_python 20/20, qa_crosscutting 15/15, qa_hook_simulation all PASS; 4 golden repos healthy — no cross-interaction regression |

**No real defects found.** Three apparent merge failures during testing were traced
to malformed synthetic fixtures (wrong idioms header, list-vs-dict archetypes shape),
not chameleon bugs — each was re-tested against the real artifact shape and passed.

---

## Closed gaps

_(none yet — closing requires human re-sign-off per the protocol)_
