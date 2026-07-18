# Chameleon — Full-Matrix Real-Usage Test Campaign

**Status:** IN PROGRESS — Phase 1 (inventory + environment)
**Branch:** `plugin-testing-fixes`
**Baseline commit:** `27fd8d3` (Release v4.4.15) — clean tree, no uncommitted changes
**Plugin version under test:** 4.4.15
**Started:** 2026-07-18

This file is the source of truth for the campaign. On any interruption, compaction, or
restart: re-read this file and `git log --oneline`, then resume from the first cell that is
not marked PASS with evidence. Never restart the campaign. Never mark a cell PASS without
fresh evidence captured in this run.

---

## 1. Environment

Verified on 2026-07-18, host `darwin 25.5.0` (arm64, Apple Silicon).

| Component | Version | Status | Notes |
|---|---|---|---|
| macOS / kernel | Darwin 25.5.0 | OK | arm64 |
| git | 2.50.1 (Apple Git-155) | OK | |
| node | v22.22.3 | OK | TypeScript/JS extractor host (`ts_dump.mjs`) |
| npm | 10.9.8 | OK | |
| pnpm | 11.12.0 | OK | |
| npx | present | OK | scaffolding |
| ruby | 3.4.9 (+PRISM) | OK | Ruby extractor host (`prism_dump.rb`) |
| prism gem | 1.9.0 / 1.5.2 (default) / 1.4.0 | OK | Ruby AST parser |
| bundler | 4.0.15 | OK | |
| rails | 8.1.3 | OK | Rails cell scaffolding |
| python3 (system) | 3.9.6 | BELOW FLOOR | `/usr/bin/python3`; below chameleon's >=3.11 floor — exercises the `_resolve-python.sh` ladder rather than blocking |
| plugin venv python | 3.13.13 | OK | `plugin/mcp/.venv/bin/python` — rung 1 of the interpreter ladder |
| uv / uvx | 0.11.7 | OK | MCP server launcher (`.mcp.json` uses `uvx`) |
| uv-managed pythons | 3.11.15, 3.12.13, 3.13.13 | OK | rung 2/3 of the ladder |
| sqlite3 | 3.51.0 | OK | `drift.db`, `index.db` |
| claude CLI | 2.1.214 | OK | real slash-command / hook invocation |
| network | reachable (npm registry 200) | OK | dependency scaffolding only; chameleon itself is offline |
| free disk | 95 GiB | OK | |

**BLOCKED items:** none. Every runtime and toolchain the plugin requires is installed and
working.

**Environment note (not a blocker, but under test):** the system `python3` is 3.9.6, below
the plugin's documented `>=3.11` floor. `plugin/hooks/_resolve-python.sh` exists precisely
for this and resolves via a validated ladder (bundled venv -> version-named binaries -> `uv
run` -> probed `python3`). This host therefore exercises the ladder's rung-1 path for real,
which is a feature of this environment, not a gap. Ladder behaviour is itself a matrix item.

### How the plugin under test is actually loaded (fix-deploy protocol)

Verified end-to-end, not assumed. The plugin that really executes in a Claude Code session is
**not** the dev working tree. There are **three** hops:

| Hop | Path | Role | State at campaign start |
|---|---|---|---|
| 1. Dev tree | `/Users/crisn/Documents/Projects/chameleon` | where fixes are authored | branch `plugin-testing-fixes` @ `16a0638` |
| 2. Marketplace clone | `~/.claude/plugins/marketplaces/chameleon` | install source | branch `main` @ `27fd8d3`, clean |
| 3. **Version-keyed cache** | `~/.claude/plugins/cache/chameleon/chameleon/4.4.15/` | **what hooks + MCP actually execute** | materialized from hop 2 |

Hop 3 was confirmed by a real `chameleon_telemetry(action="doctor")` call, which reported the
hook interpreter as:

```
hooks resolve `uv run --project /Users/crisn/.claude/plugins/cache/chameleon/chameleon/4.4.15/mcp python`
```

The cache directory is keyed by the version string in `plugin.json` — 46 historical version
dirs are present (`2.39.0` … `4.4.15`). At campaign start all three copies of
`secret_scanner.py` are byte-identical (`diff -q` clean), confirming the chain is in sync.

**Consequence — the single most important operational rule of this campaign:** editing
`plugin/` in the dev tree changes *nothing* a hook, skill, or MCP tool does. A fix only
reaches the running plugin after it is committed, propagated to the marketplace clone, **and
given a new version** so a fresh cache dir is materialized. A campaign that skipped this
would test v4.4.15 while believing it had tested its own fixes, and every post-fix "green"
would be false. This is also why the project's own `CLAUDE.md` says *"Always bump the version
— the plugin cache is version-keyed."*

Mandatory protocol after every fix cycle:

1. Commit the fix in the dev tree on `plugin-testing-fixes`.
2. `scripts/bump-version.sh <new-version>` (keeps the six manifests in sync).
3. Propagate to the marketplace clone:
   `git -C ~/.claude/plugins/marketplaces/chameleon fetch /Users/crisn/Documents/Projects/chameleon plugin-testing-fixes && git -C ~/.claude/plugins/marketplaces/chameleon reset --hard FETCH_HEAD`
4. Materialize/refresh the version-keyed cache dir for the new version.
5. Clear `~/.local/share/chameleon/interp.cache` when the interpreter ladder is touched.
6. **Assert** the running copy matches the dev tree before re-running any cell — a fix is
   never marked green against a stale plugin.

`scripts/qa-deploy.sh` (added by this campaign, dev-tooling only) implements steps 2-4 so a
cell can never be re-run against a stale plugin.

### Test workspace

Fresh repos are built under `~/Documents/Projects/chameleon-fullmatrix-qa/`, one per
language/framework cell, each `git init`-ed with a real initial commit and **no** `.chameleon/`
directory at start. The developer's own `~/.local/share/chameleon/` is never used as the
campaign's data dir; each run points `CHAMELEON_PLUGIN_DATA` at a campaign-scoped directory
so the host profile store stays untouched.

---

## 2. Inventory

_(populated in Phase 1 — see section 3 for the matrix)_

---

## 3. Coverage Matrix

### 3.1 The framework axis (10 columns)

Frameworks are language-bound, so the axis is the **language x framework** product that
actually exists in the code, not a meaningless cross-product (there is no "Rails column for
Python"). Every column below is a real classification path in
`bootstrap/orchestrator.py::_classify_framework` or a real framework-aware code layer,
verified by reading the source:

| # | Column | Language | Framework value | How the classifier reaches it (verified) |
|---|---|---|---|---|
| C1 | `ts-plain` | TypeScript/JS | `None` | no `next` dep, no `@nestjs/*` dep — proves the agnostic core |
| C2 | `ts-nextjs` | TypeScript/JS | `nextjs` | `next` in deps, or `next.config.{js,mjs,ts,cjs}` (`orchestrator.py:533-538`) |
| C3 | `ts-nestjs` | TypeScript/JS | `nestjs` | both `@nestjs/core` and `@nestjs/common` in deps (`orchestrator.py:531`) |
| C4 | `rb-plain` | Ruby | `None` | no `config/application.rb`, no `gem "rails"` — proves the agnostic core |
| C5 | `rb-rails` | Ruby | `rails` | `config/application.rb`, or `gem "rails"` in Gemfile (`orchestrator.py:485-490`) |
| C6 | `py-plain` | Python | `None` | no django/flask/fastapi dep, no django `manage.py` — proves the agnostic core |
| C7 | `py-django` | Python | `django` | `manage.py` whose *content* names django/`DJANGO_SETTINGS_MODULE` (`orchestrator.py:504-506`) |
| C8 | `py-drf` | Python | `django` **+ DRF layer** | classified `django`; DRF is a distinct *code layer*, not a distinct classification — see note |
| C9 | `py-flask` | Python | `flask` | `flask` or `flask-*` dep, no fastapi (`orchestrator.py:522`) |
| C10 | `py-fastapi` | Python | `fastapi` | `fastapi` or `fastapi-*` dep — checked first, most specific (`orchestrator.py:520`) |

**Note on DRF (verified, resolves a docs-vs-code ambiguity):** `_classify_framework` never
returns `"drf"` — a DRF repo classifies as `django`, and `principles.py:60` states this
outright (*"DRF folds into django"*). DRF is nonetheless a genuine framework-aware layer with
its own code paths, so it earns its own column rather than being folded into C7:
`signatures.py:179` (Django/DRF filename-role archetypes), `bootstrap/naming.py:303` (naming a
cluster by its Django/DRF role), `conventions.py:1705`+`2359` (Python authz-guard derivation,
the DRF/Django analog of the Rails blanket guard), `lint_engine.py:4552-4696` (APIView cohort
+ authz-guard lint), `extractors/python.py:248` (DRF base classes read from the AST). C7 vs C8
is the test that separates plain-Django behaviour from the DRF layer.

The agnostic columns (C1, C4, C6) are not filler: chameleon's core claim is that it is
framework-agnostic and learns each repo's own conventions. A column where `framework` is
`None` is the only place that claim is actually under test.

### 3.2 Matrix shape and execution strategy

The item axis is the inventory (section 2). Full coverage means **every item x every one of
the 10 columns**. Executing that as isolated one-off probes would be both enormous and
unrealistic — and the brief calls for *epic, highly complex real-world tasks*. So execution is
organised as:

- **10 epic scenarios, one per column.** Each is a multi-phase, realistic engineering
  narrative run end-to-end against that column's fresh repo, driving the full plugin
  lifecycle in order: bootstrap -> trust -> orient -> per-edit conformance -> enforcement ->
  teach -> refresh -> review -> turn-end. One epic scenario fills the large majority of its
  column's cells through genuine use, exactly as a real user would produce them.
- **Targeted probes for what a happy-path narrative cannot reach.** Degraded and damaged
  states, boundary inputs, malformed hook payloads, trust states, and lifecycle chains are
  driven explicitly per column (this is the "Pass 2 depth" discipline the project's own
  `CLAUDE.md` mandates, and where real bugs live).
- **Evidence is captured per cell**, never inferred from a neighbouring cell. A cell filled
  by an epic scenario cites the concrete invocation and output that filled it.

A cell is PASS only with (a) a real invocation, (b) captured output, and (c) a correctness
**and** effectiveness judgement. "Ran without error" is explicitly not PASS.

_(Item rows are populated once the inventory extraction completes.)_

### 3.3 The 10 epic scenarios

Each scenario is a realistic engineering narrative, run end-to-end against that column's fresh
repo. The phases are identical across columns so the matrix stays comparable, but the *work*
in each phase is native to that stack — a Rails column exercises Rails conventions, a FastAPI
column exercises FastAPI ones. Every phase names the inventory items it fills.

**Shared phase spine (all 10 columns):**

| Phase | What is driven | Primary items filled |
|---|---|---|
| P0 Cold open | `detect_repo` on an unprofiled repo; statusline; SessionStart with no profile | detect_repo, session-start, statusline, no-profile fail-open |
| P1 Bootstrap | `/chameleon-init` for real | bootstrap pipeline (discovery, extraction, clustering, canonical, naming, import graph, conventions, principles, rules, indexes, summary, transaction), framework classification, `conventions.md` mirror |
| P2 Trust | read tools while untrusted, then `/chameleon-trust` | trust gate, untrusted-injection suppression, trust token, comprehension tools under both states |
| P3 Orient | `describe_codebase`, `search_codebase`, `get_callers`/`get_callees`/`get_blast_radius`/`query_symbol_importers` | every comprehension tool, per language |
| P4 Conformance | real feature work: add a new file in an established archetype, then edit an existing one | PreToolUse Tier 1 + Tier 2 injection, canonical excerpt, archetype facts, nearby signatures, inbound callers, PostToolUse verify + lint |
| P5 Enforcement | deliberately violate a hard-class rule (credential, eval/exec, banned import); then override with `chameleon-ignore` | PreToolUse deny, PostToolUse block, escalation L0->L2, inline override, shadow/off modes |
| P6 Teach | `/chameleon-teach` a real idiom + a competing import; `/chameleon-auto-idiom` | teach_profile, teach_profile_structured, competing imports, counterexamples, idiom candidates, idiom coverage |
| P7 Drift + refresh | mutate the repo, observe drift, `/chameleon-refresh` | drift status, staleness, refresh repair, production-ref derivation, noop-vs-repair |
| P8 Review | branch with a real diff: `/chameleon-pr-review`, autopass verdict, contract breaks, duplication, dependency scan | review dispatcher actions, refuter, ledgers, cross-file context |
| P9 Turn-end | provoke the Stop pipeline: lenses, VERIFY, delivery next turn | stop-backstop, review job, advisories, finding ledger, resurface |
| P10 Depth probes | damaged/stale artifacts, malformed hook payloads, boundary inputs, trust states, lifecycle chain | fail-open behaviour, repair, robustness |

**Per-column scenario (the domain work that drives P4-P8):**

| Col | Repo | Epic scenario |
|---|---|---|
| C1 `ts-plain` | domain service, no framework | Add a settlement-reconciliation service + repository + validator across the existing archetypes, then change a shared repository signature and follow the blast radius to every caller. Proves the agnostic core with zero framework signal. |
| C2 `ts-nextjs` | Next.js App Router | Add an authenticated route group: server component, client component, `app/api` route handler, and a `lib/` service — then rename a shared lib export and repair every importer. Exercises the server/client split and Next.js roles. |
| C3 `ts-nestjs` | NestJS | Add a full feature module (controller + service + module + DTO + entity) wired into the root module, then change a service method signature consumed by two other modules. Exercises decorator-anchored archetypes and DI. |
| C4 `rb-plain` | plain Ruby gem | Add a new client + service + validator following the gem's own conventions, then change a public method used across the lib. Proves the Ruby agnostic core with no Rails signal. |
| C5 `rb-rails` | Rails 8 | Add a model + controller + service + job + serializer for a new domain concept with a migration, then alter a model association other classes rely on. Exercises the Rails-aware layer and the Rails+frontend hybrid path. |
| C6 `py-plain` | plain Python lib | Add a repository + service + client for a new bounded context, then change a shared dataclass field consumed across modules. Proves the Python agnostic core. |
| C7 `py-django` | plain Django | Add a new app (models, views, urls, forms, admin, tests) and wire it into settings/urls, then change a model field other apps query. Exercises Django roles *without* DRF. |
| C8 `py-drf` | Django + DRF | Add a viewset + serializer + permission for a new resource, then remove an authz guard from an existing viewset. The authz-guard derivation and the APIView cohort lint are the point of this column. |
| C9 `py-flask` | Flask blueprints | Add a blueprint with routes, schema, and service, register it on the factory, then change a service signature two blueprints call. Exercises the factory/blueprint pattern. |
| C10 `py-fastapi` | FastAPI | Add a router with pydantic schemas, a service, and a `Depends` dependency, then change a response model used by two routers. Exercises DI and response-model conventions. |

**Deliberate-break inventory (P5, per column).** Each column gets the same four provocations,
expressed natively: (a) a hard-coded credential, (b) an `eval`/`exec`-class dynamic execution,
(c) an import the repo's own conventions discourage, (d) a violation of the column's dominant
archetype shape. Each is run twice — once expecting the block, once with a
`chameleon-ignore` override — and once under `CHAMELEON_ENFORCE=0` to confirm advisory-only
degradation.

---

## 4. Gaps & Effectiveness Log

Running log. Every issue found during real usage, its impact, and its resolution.

### GAP-001 — `possible_aws_secret` fires on an ordinary file path in prose — OPEN

**Cell:** `secret-scan` x (language-agnostic; found on a Markdown doc)
**Severity:** advisory-noise (NOT a block — see impact)
**Found by:** genuine real usage. Chameleon's own PostToolUse hook fired on this campaign's
edit to `TESTING.md` and reported:

```
[🦎 chameleon: 1 violation]
1. detect-secrets flagged a possible_aws_secret at line 57. Never commit credentials —
   rotate the secret and move it to an environment variable or a secret manager.
```

Line 57 was a Markdown table row containing a filesystem path and a git SHA. There is no
credential on it.

**Red evidence (reproduced, not inferred):**

```
$ .venv/bin/python -c "...scan_for_secrets(<exact line 57>)..."
   HIT: possible_aws_secret line 1
Why the context gate passed:
   credential-context match = 'auth' inside the word: 'are authored) '
Why the 40-char pattern matched:
   40-char run = 'Users/crisn/Documents/Projects/chameleon' len 40
```

**Root cause — two independent defects that compound:**

1. `_CREDENTIAL_CONTEXT` (`profile/secret_scanner.py:78`) is a bare alternation with **no word
   boundaries**, so it matches inside ordinary English words. Measured: `authored`, `author`,
   `authority`, `authorize`, `authentic` (via `auth`); `monkey`, `keyboard`, `turkey`,
   `donkey`, `whiskey`, `keynote` (via `key`); `accessible`, `accessory` (via `access`);
   `privately` (via `private`); `secretary`, `secretly` (via `secret`); `tokenize`,
   `passwordless`.
2. The `possible_aws_secret` pattern (`secret_scanner.py:22`) is `\b[A-Za-z0-9/+=]{40}\b` —
   the class includes `/`, so **any 40-character filesystem path matches**. Here
   `Users/crisn/Documents/Projects/chameleon` is exactly 40 chars.

Either alone is harmless; together, a prose line that mentions an *author* next to a
40-char *path* is reported as a leaked AWS credential.

**Impact / effectiveness:** the rule is deliberately **advisory-only** — `possible_aws_secret`
is explicitly excluded from `_DETERMINISTIC_SECRET_KINDS` (`violation_class.py:294`), so it
can never block an edit. The damage is precision, not availability: the user is told to
"rotate the secret" for a file path, which is exactly the false-positive noise the
`_CONTEXT_GATED_KINDS` gate (`secret_scanner.py:69-76`) was introduced to eliminate. The gate
is under-precise, so it is not doing the job its own comment claims. This is a defect in the
mitigation, not accepted behaviour.

**Fix:** deferred to the fix phase (word-boundary the credential-context gate; consider
excluding path-shaped runs from the 40-char pattern). Must be re-verified across all
languages since the scanner is language-agnostic.

---

---

## 5. Fix Log

_(one entry per fix cycle: issue, cell, root cause, red evidence, green evidence, commit)_
