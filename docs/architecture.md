# chameleon architecture

> *"Code that blends in."*

This document describes how chameleon works as built. It is the reference for
the bootstrap pipeline, the hook stack, the MCP tool surface, the enforcement
and review gate, the profile schema, the state stores, and the security model.
It tracks engine version **3.6.0** and profile **schema version 8**. When the
code and this document disagree, the code is right; please file an issue.

## Contents

- [Purpose and honest boundary](#purpose-and-honest-boundary)
- [Vocabulary](#vocabulary)
- [System overview](#system-overview)
- [Repository layout](#repository-layout)
- [The profile](#the-profile)
- [Bootstrap and refresh pipeline](#bootstrap-and-refresh-pipeline)
- [Production-ref derivation](#production-ref-derivation)
- [Cluster signature function](#cluster-signature-function)
- [Canonical selection](#canonical-selection)
- [Conventions derivation](#conventions-derivation)
- [Cross-file indexes](#cross-file-indexes)
- [Hook stack](#hook-stack)
- [MCP server (chameleon-mcp)](#mcp-server-chameleon-mcp)
- [Enforcement](#enforcement)
- [The review gate and trust path](#the-review-gate-and-trust-path)
- [Trust model](#trust-model)
- [Atomicity, locking, and crash safety](#atomicity-locking-and-crash-safety)
- [State stores](#state-stores)
- [The advisor daemon](#the-advisor-daemon)
- [Security model](#security-model)
- [Performance characteristics](#performance-characteristics)
- [Configuration and environment](#configuration-and-environment)
- [Versioning and schema migrations](#versioning-and-schema-migrations)
- [What stays human](#what-stays-human)
- [Glossary](#glossary)

---

## Purpose and honest boundary

chameleon gives the model deep knowledge of *your* repo's conventions: not a
list of pre-known framework patterns, but the patterns your team actually
wrote. It clusters AST and statistical signals from the codebase, captures the
idioms an AST cannot infer through `/chameleon-teach`, and injects
archetype-keyed guidance on every edit.

**North star:** a machine review gate good enough that human review moves from
mandatory to on-demand for the change classes the engine provably covers.
chameleon does not certify correctness, and no amount of static AST plus
statistics ever will. What it does is cover the review classes it covers well,
reserve human eyes for the classes it cannot, and give a team lead the evidence
to trust that split.

**What the gate covers, deterministically and per-repo calibrated:** structural
and convention conformance, hallucinated imports and symbols, hardcoded
secrets, supply-chain red flags in manifests and lockfiles, and cross-file
symbol-existence breaks. On top of that it runs an advisory LLM judge at
PR-review and turn-end that reads the diff for logic-delta regressions (a
removed guard, a dropped await, an inverted condition).

**What stays human, always:** business-logic correctness, novel security flaws
(authorization logic, IDOR, complex injection, crypto design), and
architectural judgment. The LLM judge helps on these; it is not a principal
engineer, so it advises and never gates alone. Anything in an unsupported
language, below a sample-size threshold, or in a file class the engine does not
verify goes to a human regardless of gate color. See
[What stays human](#what-stays-human) for the full list.

**Supported stacks:** TypeScript / JavaScript, Ruby, and Python as first-class
languages. Claude Code only. The core is framework-agnostic: it learns each
repo's conventions from the repo's own structure (clustering, naming,
signatures), so it works on any framework, not just well-known ones. Where a
framework has strong, well-known conventions, chameleon adapts for deeper,
framework-aware guidance: Rails for Ruby; Django, DRF, Flask, and FastAPI for
Python; and Next.js and NestJS for TypeScript / JavaScript. The TS framework
layer is lighter than the Rails/Django ones (framework detection, naming roles,
and framework-specific anti-hallucination guidance, rather than the full
guard/contract derivation), but it is no longer absent.

All three languages are first-class at the extractor level: the TypeScript
extractor uses the TypeScript Compiler API, the Ruby extractor uses Prism, and
the Python extractor uses libcst (bundled with the plugin, so Python repos need
nothing extra installed). Python framework awareness keys on filename
conventions (`models.py`/`views.py`/`serializers.py` → cross-app role
archetypes) and the web-layer directory (`routes/`, `blueprints/`), with
decorators and base classes captured for finer discrimination.

---

## Vocabulary

Five user-facing terms carry the whole model:

- **profile** the team's conventions captured in `.chameleon/`, committed to git.
- **archetype** a category of file with shared patterns (controller, service, hook, component, worker).
- **idiom** a team-specific rule or banned pattern that an AST cannot infer.
- **refresh** automated re-analysis (`/chameleon-refresh`).
- **trust** per-user approval of a committed profile (`/chameleon-trust`).

Internal terms (`canonical`, `witness`, `normative shape`, `content_signal`,
`cluster signature`, `recency weight`) appear in this document and the code, not
in user-facing copy. The context tag is neutral: `<chameleon-context>`, with no
importance framing, so it never competes with other plugins' framing.

---

## System overview

chameleon has two halves: the **engine** (one plugin, shipped to every user)
and the **profile** (per-repo data, committed to each repo). The engine carries
no repo-specific knowledge; the profile carries no code.

```
+--------------------------------------------------------------------------+
| Engine (the chameleon plugin)                                            |
|                                                                          |
|  Hooks (subprocess-per-call, fail-open)     Skills (static prose)        |
|  - session-start        (SessionStart)      - using-chameleon (auto)     |
|  - preflight-and-advise  (PreToolUse)       - 14 user-invocable /commands|
|  - posttool-recorder     (PostToolUse)                                   |
|  - posttool-verify       (PostToolUse)                                   |
|  - callout-detector      (UserPromptSubmit)                              |
|  - stop-backstop         (Stop / SubagentStop)                          |
|                              |                                           |
|                              v                                           |
|  MCP server (chameleon-mcp, FastMCP, stdio) -- 19 tools                  |
|                              |                                           |
|              +---------------+----------------+                          |
|              v                                v                          |
|  AST extractors                     Bootstrap / refresh pipeline         |
|  - ts_dump.mjs    (TS Compiler API) detect -> discover -> parse ->       |
|  - prism_dump.rb  (Prism)           cluster -> canonical -> conventions  |
|  - libcst_dump.py (libcst CST)      -> atomic commit                     |
+--------------------------------------------------------------------------+

Per-repo, committed to git:           Per-user, local only (never committed):
<repo>/.chameleon/                    ~/.local/share/chameleon/
  profile.json, archetypes.json,        index.db (repo registry)
  canonicals.json, conventions.json,    <repo_id>/
  rules.json, idioms.md, principles.md,   drift.db, .trust,
  conventions.md (memory-channel mirror),
  config.json, *index*.json, ...          .pause_until, prodtree/, markers
```

The engine talks to the model only through hooks. Hooks call the MCP server,
the MCP server reads the profile, and the result is injected as a
`<chameleon-context>` block. The model never has to call an MCP tool by hand,
though the same tools are available to it and to the `/chameleon-*` skills.

chameleon ships one auto-fired skill (`using-chameleon`) and fourteen
user-invocable slash commands (14 commands); the full list is in the
[README](../README.md#slash-commands).

A per-user **advisor daemon** (POSIX only) is an optional performance layer that
holds the profile in memory and answers hot-path hook lookups over a unix
socket. It is never load-bearing: every hook falls back to an in-process lookup
if the daemon is absent. See [The advisor daemon](#the-advisor-daemon).

---

## Repository layout

```
chameleon/
├── .claude-plugin/
│   └── marketplace.json           # marketplace entry (source: ./plugin)
├── plugin/                        # the installable plugin surface — what a marketplace install copies
│   ├── .claude-plugin/
│   │   └── plugin.json            # plugin manifest (version anchor)
│   ├── .mcp.json                  # launches chameleon-mcp via uvx
│   ├── hooks/
│   │   ├── hooks.json             # hook registrations
│   │   ├── run-hook.cmd           # cross-platform polyglot dispatcher
│   │   ├── _resolve-python.sh     # interpreter resolution ladder (>=3.11)
│   │   ├── session-start
│   │   ├── preflight-and-advise
│   │   ├── posttool-recorder
│   │   ├── posttool-verify
│   │   ├── callout-detector
│   │   └── stop-backstop
│   ├── skills/
│   │   ├── using-chameleon/       # auto-fired foundation skill
│   │   └── chameleon-*/           # 14 user-invocable slash commands
│   ├── agents/                    # code-scout, pattern-reviewer, web-researcher
│   ├── mcp/
│   │   ├── pyproject.toml         # Python package (requires-python >=3.11)
│   │   ├── uv.lock, package.json  # committed locks
│   │   ├── typescript-checksums.json  # build-time SHA-256 manifest (not verified at runtime)
│   │   └── chameleon_mcp/         # the Python package (see below)
│   ├── scripts/                   # runtime scripts shipped with the plugin
│   │   ├── ts_dump.mjs            # TypeScript AST extractor (Node)
│   │   ├── prism_dump.rb          # Ruby AST extractor (Prism)
│   │   ├── libcst_dump.py         # Python AST extractor (libcst)
│   │   ├── chameleon-merge-driver.sh  # git merge driver for .chameleon
│   │   └── setup.sh               # prerequisite check + dependency warm-up
│   └── bin/
│       └── chameleon-statusline.sh    # status line (<100ms budget)
├── scripts/                       # dev-only tooling (not shipped)
│   ├── bump-version.sh            # keeps six manifests in sync
│   └── ...
├── tests/                         # unit/, journey/, effectiveness/, qa_*.py
└── docs/                          # architecture.md, install.md, qa-team.md
```

The Python package (`plugin/mcp/chameleon_mcp/`) is the brain. The load-bearing
modules: `server.py` (FastMCP tool registry), `tools.py` (tool implementations),
`hook_helper.py` (the hook dispatch entry point and all gate logic),
`bootstrap/` (the derivation pipeline), `extractors/` (language dispatch),
`profile/` (schema, loader, config, trust), `conventions.py` and `lint_engine.py`
(convention derivation and linting), `enforcement.py` and
`enforcement_calibration.py` (the block gate and its calibration), `judge.py`
and friends (the advisory review layer), and the cross-file index modules
(`symbol_index.py`, `calls_index.py`, `function_catalog.py`,
`symbol_signatures.py`).

---

## The profile

A profile is a directory of committed artifacts plus a set of per-user local
files. The committed half is what makes conventions team-shared and reviewable
in a PR.

### Committed artifacts (`<repo>/.chameleon/`)

All JSON artifacts carry `schema_version`, `engine_min_version`, and a
`generation` counter. They are written together inside one atomic transaction.

| File | Contents |
|---|---|
| `COMMITTED` | Sentinel written last; loaders refuse the profile without it. |
| `profile.json` | Manifest: repo_id, language, source, archetype count, workspace block, tool-config sources, `derivation_source` provenance. |
| `archetypes.json` | Per-archetype cluster facts: id, size, path pattern, content signal, top-level node kinds, export shape, optional sub-buckets, summary. |
| `canonicals.json` | Per-archetype canonical: the witness (path + sha hint), normative shape (AST query + callable signatures), normative idioms (comments), and the secret/injection/poisoning scan verdicts. |
| `conventions.json` | Per-archetype derived conventions (imports, naming, error handling, body shape, doc coverage, test pairing, inheritance and method calls for Ruby, class contract, key exports) plus repo-level layering. |
| `principles.md` | Data-backed prose principles generated from conventions. |
| `conventions.md` | The CLAUDE.md-channel mirror: the rendered conventions block PLUS the principles sections and a TEAM IDIOMS gist section (one `- name: first-sentence directive` line per active idiom, rendered from the `idioms/` store), for wiring into Claude's memory channel via a one-line `.claude/rules/chameleon-conventions.md`, `CLAUDE.local.md`, or a `CLAUDE.md` import (init offers all three, consent-gated; none edits an existing file by default). Rewritten by bootstrap/refresh, re-synced by teach/unteach after every conventions.json or idioms.md mutation, self-healed from disk on a noop refresh when missing or older-format; absent when nothing renders. Kill switch `CHAMELEON_CONVENTIONS_MD=0`. Motive: memory-channel delivery measured 100% rule adherence vs 40% for the same content as a hook advisory (results-published/migration-ab-2026-07-11.md). Because the mirror is the complete session-conventions content, a wired import lets SessionStart skip its duplicate hook injection (`CHAMELEON_MEMORY_CHANNEL_DEDUP`). (Pre-3.4.0 this also let the Stop idiom-review interrupt render mirror-carried idioms as gists via `CHAMELEON_STOP_IDIOM_GIST`; that interrupt is gone — idiom review is now the async job's idiom lens, see [Turn-end advisories](#turn-end-advisories-on-by-default-never-block).) |
| `rules.json` | Tool-derived rules keyed by source: prettier, tsconfig compiler options, eslint, editorconfig, rubocop. |
| `idioms/` | The idiom store: one schema-validated JSON file per idiom (`core/idiom_store.py`, schema `chameleon-idiom-1`), the single source of idiom truth. Each record carries a slug, title, body, scope, status (`active`/`deprecated`), and source (`taught`/`auto`/`learned`). Per-file storage keeps git merges trivial and scopes the injection scan to one idiom. `core/idiom_store.py` is the only reader/writer. |
| `idioms.md` | A generated *view* of the `idioms/` store (rendered by `regenerate_views`), not a source of truth. A first run writes an empty template; a legacy hand-authored `idioms.md` migrates into the store on the next teach (`migrate_idioms_md`, `records_from_markdown`). Kept for human review and PR diffs. |
| `idiom-candidates/` | The self-learning miner's never-auto-adopted proposals: one `<slug>.json` per candidate with an evidence trail. Deliberately **not** part of the trust-hashed surface (`_HASHED_ARTIFACTS`) — hashing unreviewed output would arm the trust gate on the miner itself. A candidate becomes a real idiom only through the same `/chameleon-teach` path a hand-taught idiom uses. See [Self-learning idiom miner](#self-learning-idiom-miner). |
| `profile.summary.md` | Human-readable summary for PR review and the trust prompt. |
| `enforcement.json` | Per-rule block-calibration verdict (`{rule: {active, fp_rate, sampled, flagged}}`; the two calibration-exempt security rules also carry `exempt_reason: "security-rule"` and are active regardless of this artifact). |
| `exports_index.json`, `reverse_index.json` | Symbol export map and its inverse importer graph. TS/JS + Python (`REVERSE_INDEXED_LANGUAGES`). |
| `function_catalog.json` | Per-function name, shape, and body-hash for the duplication prefilter. All three languages. |
| `calls_index.json` | Deterministic caller -> callee edges for the judge. All three languages. |
| `symbol_signatures.json` | Per-callable signature and body span for forward-definition hydration. All three languages (declared types TS only). |
| `constant_index.json` | Ruby-only: per-constant reverse index mapping constant references to their defining class, backing the cross-file call-site analysis. |
| `counterexamples.json` | Per-archetype off-pattern counterexample: a real instance of a taught discouraged import, paired with the witness at edit time as a "do NOT write it this way" example. Built at teach time and bootstrap/refresh; drop-stale. |
| `renames.json` | User archetype-rename overlay (written only when a rename map exists). |
| `config.json` | Operator-managed: `production_ref`, `auto_refresh`, `enforcement`, `trust`, `canonical_ref`. Read, never produced, by bootstrap. |

### Per-user local state (`~/.local/share/chameleon/`)

Overridable with `CHAMELEON_PLUGIN_DATA`. Never committed, never exfiltrated.

| Path | Contents |
|---|---|
| `index.db` | SQLite registry of every repo this user has bootstrapped. |
| `<repo_id>/drift.db` | Per-edit confidence history, override audit, decision log, finding ledger. |
| `<repo_id>/.trust` | Per-user trust grant for this repo. |
| `<repo_id>/.pause_until` | `/chameleon-pause-15m` expiry (line 1) + HMAC `sig=` line; with the per-user key available an unsigned/forged marker is ignored, so a live pre-4.0.1 marker stops suppressing after upgrade. |
| `<repo_id>/.session_disabled.<hash>` | HMAC-signed per-session disable marker. |
| `<repo_id>/prodtree/` | Materialized production-branch worktrees (swept after use). |
| `<repo_id>/.intent.<session>.ndjson` | Captured intent tokens and digests (never raw prose). |
| `<repo_id>/review_ledger.ndjson`, `session_attestations.ndjson` | HMAC-signed review and attestation records. |
| `<repo_id>/findings_ledger.json` | The canonical turn-end Finding-lifecycle ledger (`review_ledger.py`): one row per finding `match_key`, walking `pending`/`delivered`/`addressed`/`resurfaced`/`shelved`/`expired`. Local, not committed. See [Turn-end advisories](#turn-end-advisories-on-by-default-never-block). |
| `<coordinator repo_id>/cross_reverse_index.json` | Monorepo cross-workspace import edges (deliberately off the trust-hashed profile surface). |

The Bash exec log lives separately under `${TMPDIR:-/tmp}/.chameleon_exec_log/<repo_id>/`
(mode 0700), and the HMAC key under `~/.claude/hooks/.exec_hmac.key`
(mode 0600, overridable with `CHAMELEON_HMAC_KEY_PATH`).

---

## Bootstrap and refresh pipeline

`/chameleon-init` calls `chameleon_lifecycle(action="bootstrap_repo", ...)`;
`/chameleon-refresh` calls `chameleon_lifecycle(action="refresh_repo", ...)`.
Both delegate to the same orchestrator, which runs once for the
repo root and once per detected monorepo workspace.

Before the pipeline runs, the tool wrapper applies three guards: it refuses an
unsafe root (a temp-dir or world-writable directory, override
`CHAMELEON_ALLOW_TMP_REPO=1`), warns on a non-git parent with git children, and
sweeps orphaned transaction directories from crashed prior runs. When a
`production_ref` lock exists it resolves the ref and materializes a worktree to
analyze (see [Production-ref derivation](#production-ref-derivation)).

The pipeline stages, in order:

1. **Workspace detection.** Detect pnpm/yarn/lerna/turbo/nx workspaces and
   monorepo layouts; fan out to each workspace root.
2. **Tool-config read.** Read eslint, prettier, tsconfig, rubocop, editorconfig.
3. **Language detect.** Select the extractor, first match wins. TypeScript wins
   on a `tsconfig.json`, or a `package.json` naming `typescript`/`ts-node`/`vite`,
   or a bounded scan of `.ts`/`.tsx` files. Ruby wins on a `Gemfile` or any
   `*.gemspec`. Python wins on a project marker
   (`pyproject.toml`, `setup.py`, `setup.cfg`, `requirements.txt`, `manage.py`,
   `Pipfile`, `tox.ini`) or, absent all of them, any `*.py` file in the tree. No
   supported signal yields `failed_unsupported_language`.
4. **Discovery and exclusion.** Enumerate candidate source files, excluding
   generated, vendored, and test directories from the canonical pool. A
   post-exclusion ceiling of 200k files raises `TooManyFilesError`; large repos
   pass an explicit `paths_glob`.
5. **AST parse.** Run the extractor over the candidates. A missing language
   toolchain degrades to a typed report naming the actual missing toolchain
   (`failed_node_unavailable` for TS, `failed_ruby_unavailable` for Ruby,
   `failed_python_unavailable` for Python, with a generic
   `failed_extractor_unavailable` fallback), not a crash. A corpus that parsed
   too poorly degrades to `failed_extractor_degraded`.
6. **Clustering.** Group files by their [cluster signature](#cluster-signature-function),
   then run loose-merge, shape-fuzzy-merge, and sub-bucket-split passes.
   Generated files are skipped here.
7. **Canonical selection.** For each dense cluster, pick the witness that passes
   all [security scans](#canonical-selection). Sparse clusters never reach this
   stage.
8. **Archetype assembly and naming.** Build archetype and canonical entries,
   derive the normative AST query, and propose archetype names.
9. **Conventions derivation.** Re-read members for declarations and derive
   per-archetype conventions; merge taught competing imports from the prior
   profile.
10. **Rules assembly.** Convert tool configs to `rules.json`.
11. **Idiom store carry-forward.** The `.chameleon/idioms/` store is idiom
    truth and is carried forward untouched; its `idioms.md` and conventions.md
    views are re-rendered from it. A first run writes an empty template; a
    legacy hand-authored `idioms.md` is migrated into the store on the next
    teach.
12. **Atomic commit.** Write every artifact and index, fsync, write the
    `COMMITTED` sentinel last, and flock-serialize the directory rename. See
    [Atomicity](#atomicity-locking-and-crash-safety).
13. **Drift baseline.** Record the post-commit baseline, best-effort.

### Extractors and limits

All three extractors are long-lived subprocesses fed file paths on stdin,
emitting NDJSON on stdout, read under a 600-second wall-clock timeout. They are
spawned from a neutral working directory with the interpreter's
startup-injection environment scrubbed so they never load repo-controlled
startup code: Ruby drops `RUBYOPT`/`RUBYLIB`; TypeScript pops `NODE_OPTIONS`
(so a poisoned `--require` cannot preload code) and sets
`NODE_PATH`/`CHAMELEON_NODE_MODULES` itself; Python drops
`PYTHONPATH`/`PYTHONSTARTUP`.

Per-file ceilings are application-level, enforced inside the extractor:
`MAX_AST_NODES` (50,000 for TS and Ruby; 165,000 for Python, because libcst CST
nodes are roughly 3.3x denser and an equal ceiling would unfairly drop
equivalent Python files), `MAX_FILE_SIZE = 1_000_000` bytes,
`MAX_PARSE_DIAGNOSTICS = 20`, `MAX_CALLABLE_SIGNATURES`, and `MAX_CALL_SITES`.
A symlink is refused; an oversize or too-broken file is skipped and marked so
the partial corpus stays visible. There is no OS-level RSS or CPU rlimit and no
`--max-old-space-size`; the bound is the node/byte ceilings plus the wall clock.

The TypeScript toolchain is provisioned at first use, not vendored. The first
`/chameleon-init` on a TS repo runs `npm ci`/`npm install` into a per-user,
version-scoped directory under the data dir. `plugin/mcp/typescript-checksums.json` is
a SHA-256 manifest generated by `scripts/generate-typescript-checksums.sh` (dev tooling, repo root). It
is a build-time integrity reference only; it is **not** cryptographically
verified on the extraction hot path.

---

## Production-ref derivation

Bootstrap and refresh derive the profile from the repo's **production branch
tree**, not the checked-out working tree, whenever a production lock exists. The
lock is `production_ref` in `config.json` (a branch name; resolution prefers
`origin/<name>` over the local branch). This means the profile reflects the
production line no matter which feature branch a teammate has checked out.

Lock precedence, highest first:

1. **Explicit** `chameleon_lifecycle(action="bootstrap_repo", params={"production_ref": ...})`,
   the init skill's confirmed answer. Always wins, always persisted.
2. **Persisted** `production_ref` in `config.json`.
3. **Auto-detection** (init and the refresh migration): the remote's declared
   default branch (`refs/remotes/origin/HEAD`), then an origin branch named
   `production`/`prod`, then `main`/`master`/`trunk`. Auto-locking requires the
   answer to be unambiguous **and** origin-backed; a local-only repo keeps
   working-tree derivation and the skill asks rather than silently flipping
   semantics. An explicit `"production_ref": null` is a durable opt-out that
   auto-detection never overrides.

Mechanics: the locked ref's tip is resolved from local objects, the tree is
materialized with `git worktree add --detach` under
`<data>/<repo_id>/prodtree/<sha12>-<pid>`, and the whole pipeline runs against
that tree. Materialization passes `-c core.hooksPath=<devnull>` so the repo's
post-checkout hook never runs: a derivation that promises static analysis must
never execute repo-controlled code. Worktrees from crashed runs are swept at the
next prepare, skipping any whose creating PID is still alive. The orchestrator
splits the **analysis root** (the materialized tree) from the **write/identity
root** (the real checkout): the profile dir, repo_id, drift baseline, and prior
idioms all bind to the real checkout, and persisted paths are repo-relative so
artifacts apply one-to-one. `profile.json` records `derivation_source`
(`{mode, branch, ref, sha}`) as provenance.

**Default-on fetch.** When a locked branch is origin-backed, refresh (manual and
auto) runs one bounded, non-interactive `git fetch origin <branch>` *before*
resolving the tip, so derivation sees the genuinely latest production. The fetch
is hardened against argument injection: the branch name is validated before it
reaches argv, credential helpers and SSH prompts are disabled, and a stuck
transfer is killed by process group. It self-suppresses under `CI`, never runs
on a hook hot path, and fails open to the last-fetched ref with a classified
reason. Kill switch: `CHAMELEON_FETCH_PRODUCTION_REF=0` or config
`auto_refresh.fetch_production_ref=false`. Tuning:
`CHAMELEON_PRODUCTION_REF_FETCH_TIMEOUT_SECONDS` (default 10) and
`CHAMELEON_PRODUCTION_REF_FETCH_BACKOFF_HOURS` (default 6, applied only after an
auth or branch-gone failure).

**Staleness for a pinned repo is the tip SHA, not mtimes.** If the recorded
`derivation_source.sha` equals the current tip, refresh is a `noop` (feature
churn is irrelevant). If the tip moved, refresh re-derives from the new tree.
The SessionStart auto-refresh has a matching trigger, and a
`[🦎 chameleon: production drift]` banner surfaces the pending staleness.
`get_drift_status` carries a `production_ref` block (derived_sha, tip_sha,
tip_moved, commits_ahead).

Every failure mode (no git, no origin, unresolvable ref, worktree-add failure)
degrades to working-tree derivation with a note in the envelope. The lock is
best-effort, never a new hard dependency. `canonical_ref` is orthogonal:
`production_ref` governs what analysis derives *from*; `canonical_ref` redirects
which committed `.chameleon` snapshot the hooks *read*.

---

## Cluster signature function

An archetype is a named cluster of files that share a structural signature. The
signature `sig: file -> ClusterKey` is computed in a single AST pass. The
`ClusterKey` dataclass declares seven fields for shape and JSON compatibility,
but one of them is degenerate by construction, so the **live discriminating
signature is six dimensions**:

1. **`path_pattern_bucket`** the depth- and monorepo-aware path glob, with the
   file extension appended during clustering.
2. **`content_signal_match`** the first-200-byte lexical directive
   (`use_client`, `use_server`, a shebang, a TS pragma, or none).
3. **`top_level_node_kinds`** the set of top-level AST node kinds, stored as a
   sorted, deduplicated set (order- and multiplicity-insensitive, to agree with
   the runtime lint conformance check).
4. **`default_export_kind`** the kind of the default export, or none.
5. **`named_export_count_bucket`** bucketed named-export count (0, 1, 2-4, 5-9, 10+).
6. **`jsx_present`** whether JSX appears anywhere in the file.

The seventh declared field, `import_module_set_hash`, is hardcoded to the empty
string. The exact import set was the single largest source of
over-fragmentation (it made each service its own cluster), so it was dropped in
schema v8; import conventions are derived separately. The field survives only
for the dataclass and JSON shape.

After exact-key clustering, three passes refine the result: a loose merge of
sparse clusters by Jaccard overlap, a shape-fuzzy merge of near-identical
clusters, and a sub-bucket split that records `sub_buckets` when one archetype
spans varied concerns.

**Stability obligations.** Running `/chameleon-refresh` twice on the same input
produces a byte-identical profile (idempotence). Adding or removing a single
file does not flip canonical selection unless that file *is* the new canonical.
The profile-wide invalidation lever is `CURRENT_SCHEMA_VERSION`, not a separate
signature-function version.

---

## Canonical selection

A canonical is the reference example for an archetype. It is **trichotomized** so
the engine is explicit about what part of the example is normative:

- **Witness** the actual file (path plus xxhash64 sha hint). It has the team's
  real idioms and also its idiosyncrasies.
- **Normative shape** the AST query the archetype must match, plus the consensus
  callable signatures.
- **Normative idioms** prose annotations (leading comments and any taught
  idioms) that capture intent.

The canonical excerpt injected at edit time is a **witness, not a template**:
match its shape and idioms, not its specific business logic.

Selection walks the cluster's eligible members sorted by `(demoted,
-recency_weight, -typicality, path)`. **Demotion is the top-priority key**: it
pushes structural non-representatives below their concrete siblings regardless of
how recently they were committed, a Rails `application_*.rb` abstract base, an
imports-only NestJS `@Module` aggregator, and a migration whose
`ActiveRecord::Migration[x.y]` version is behind the cluster's current one. Below
it, **recency weight** decays smoothly off each file's **last git commit time**:
a single `git log` walk per bootstrap builds a `{path: commit_epoch}` map, and the
weight is the full multiplier (2.0) for a just-committed file, halving its boost
above 1.0 every `CANONICAL_RECENCY_HALF_LIFE_DAYS` (default 45). Commit time
survives a fresh clone's uniform mtimes, so a recently committed minority idiom
outranks the legacy majority, the exact case a mid-migration repo needs; a commit
ahead of the clock (cross-machine skew) is clamped to most-recent, never
penalized. When git is unavailable or a file is untracked, the weight falls back
to the legacy mtime step (2.0 within 90 days, else 1.0); `CHAMELEON_CANONICAL_GIT_RECENCY=0`
forces that fallback. **Typicality** (closeness to the cluster's most common AST
shape) breaks a same-commit tie, then the path string decides. The first member
that passes all three security scans wins. If none pass, the cluster is flagged
`clusters_with_only_failing_canonicals` so the gap is visible rather than
silently shipping a poisoned example.

The three scans, run during selection:

- **Secret scan** runs `detect-secrets` when available plus a deterministic
  regex set (AWS, GitHub, GitLab, AI keys, Stripe, Slack, Google, GCP service
  accounts, Azure, private keys, high-entropy hex). Any hit excludes the file.
- **Injection scan** flags instruction-shaped prose ("you must", "ignore
  previous instructions", literal `<system>` or `<chameleon-context>` tags) so a
  committed file cannot smuggle a prompt into the model's context.
- **Poisoning scan** flags dangerous code shapes: raw SQL string concatenation,
  `eval`/`exec` calls, `subprocess(shell=True)`, and security-context weak hash
  or insecure random.

Supply-chain checks are not part of canonical selection; they run only at tool
time (see [The review gate](#the-review-gate-and-trust-path)).

---

## Conventions derivation

Beyond the cluster signature, bootstrap derives per-archetype conventions into
`conventions.json`. Each section is gated on its own sample-size floor and, where
applicable, a 0.60 dominance frequency: a convention is the archetype's norm
only when the clear majority of its members share it. Sections that do not clear
their gate stay empty. All thresholds live in `_thresholds.py`.

- **`naming`** dominant identifier casing (Ruby methods/classes/constants),
  TypeScript interface prefix, and the dominant file-basename casing and suffix.
  The file-naming convention is block-eligible under calibration.
- **`import_ordering`** the dominant external-versus-relative grouping order
  (advisory NIT; competes with deterministic formatters, so low impact).
- **`error_handling`** the archetype's error-handling shape at 0.60 (TS
  try/catch fraction; Rails controller-base `rescue_from`). Feeds a principle.
- **`body_shape`** percentiles of branch count, nesting depth, line span, and
  parameter count, from a thicker witness pool. Branch count and nesting are the
  primary outlier signal; line span and parameter count never trigger alone.
  Advisory only.
- **`doc_coverage`** the fraction of public declarations with a leading doc
  comment. Surfaced as a NIT.
- **`test_pairing`** the fraction of source files with a paired test at the
  derived path. Advisory only and never block-eligible: too many files
  legitimately lack a test for the near-zero-FP gate to keep a block rule
  active.
- **`callable_signatures`** the consensus positional shape of callable names an
  archetype shares (a name must appear in at least two members). Used only at
  PR-review with the LLM judge; no per-edit arity lint.
- **`inheritance`** Ruby and Python: the archetype's dominant base class (a
  Rails controller's `ApplicationController`, a Django model cohort's
  `models.Model`). **`method_calls`** Ruby-only: shared DSL macro calls.
- **`required_guards`** Ruby controllers: the shared `before_action` guard
  symbols, accounting for `skip_before_action` and scoping. Advisory, because
  Rails authz is routinely inherited.
- **`class_contract`** the archetype's required class-body shape (base, macros,
  decorators, required methods).
- **`layering`** the cross-cluster import-edge multiset, from which the engine
  derives forbidden-upward edges only for directional, unanimous pairs, plus a
  static cycle report. Advisory.

---

## Cross-file indexes

Six committed index artifacts make the cross-file checks possible without
re-parsing callers on the hot path. All key on repo-relative paths, are
byte-reproducible, are hashed into the trust SHA, and fail open to "no facts"
(never a crash, never a fabricated claim) on any corruption.

- **`exports_index.json`** (TS/JS + Python): each source path to the set of
  names it exports. A file with `export * from` is marked open and skipped by
  the phantom-symbol check, since barrel files are the dominant false-positive
  source.
- **`reverse_index.json`** (TS/JS + Python): the inverse view, exported-name to the
  files that import it by name plus the import line. Backs the edit-time
  blast-radius advisory and the cross-file symbol-existence check (a name that
  was exported is gone and an indexed importer still references it).
- **`function_catalog.json`** (all three languages): per function, the name, kind,
  arity, and two body hashes (plain and parameter-normalized). The body hash
  drops the name line, collapses whitespace, and hashes the rest, but only for
  bodies past a minimum length. No body text is stored. This is the cheap
  candidate-narrowing layer for cross-file duplication; the LLM caller judges
  equivalence against real bodies.
- **`calls_index.json`** (all three languages): callee file to callable name to
  recorded caller rows. It stores exactly five deterministic grades and never
  name-only repo-wide matches (the false-positive bulk):
  - `same_file` a bare call to a same-file callable, or a `this.`/`self.` call
    to a same-file class member.
  - `import` (TS and Python) a call of a named import matched on its local
    binding and recorded under the exported name, where the callee exists in the
    target's closed export set.
  - `constant_receiver` (Ruby only) `Const.method` where `Const` resolves to
    exactly one defining class and the member is class-level.
  - `typed_property` (TS only) `this.<prop>.<method>()` where the property's
    declared type (a constructor parameter-property or typed field) resolves to
    a class that records the method as a member — the dependency-injection call
    shape (NestJS/Angular constructor injection). Any resolution gap (generics,
    unions, untyped fields, open barrel exports) yields no edge.
  - `module_attribute` (Python only) `mod.func()` where `mod` is a submodule
    bound by `from pkg import mod` resolving to a real in-repo module file, and
    the member is a callable defined in that file — the Python analog of
    `typed_property`/`constant_receiver`.
  Each entry carries an honest `total` and a `truncated` flag so a capped count
  reads as a lower bound, not an undercount. Like every protocol artifact (see
  [Atomicity](#atomicity-locking-and-crash-safety)), a failed rebuild drops the
  old copy rather than carrying it forward: stale caller facts fed to the judge
  are worse than none.
- **`symbol_signatures.json`** (all three languages): per callable, the parameter
  shape, declared types (TS only), and body span, for the judge's
  forward-definition hydration.
- **`constant_index.json`** (Ruby only): each referenced constant to its defining
  class, so a `Const.method` call site resolves to exactly one class for the
  cross-file blast-radius and call-site checks.

The primary consumer is the turn-end correctness judge, which renders a bounded
caller-facts block (including multi-hop transitive callers) for the callables a
diff changed. Absence of an edge is never evidence of dead code; the facts block
says so explicitly. The same artifacts back `get_callers`,
`query_symbol_importers`, `get_crossfile_context`, and `get_duplication_candidates`.

A seventh index lives outside the committed profile: the monorepo
**cross-workspace existence index**, `cross_reverse_index.json` (default on,
kill switch `CHAMELEON_CROSSWS_INDEX=0`). `build_reverse_index` runs per
workspace, so a file in package B importing from package A over a
`@scope/a`-style specifier is invisible to A's own reverse index, and a removed
export a sibling package still imports goes unseen. At bootstrap and refresh
each workspace captures the cross-package import specifiers its own reverse
index drops, and the coordinator join resolves each `@scope/pkg` specifier to
the sibling workspace's file through a `package.json`-name map with a
fail-closed name-in-exports confirmation. The resolved edges are written to the
plugin data dir (`<data>/<coordinator repo_id>/cross_reverse_index.json`),
deliberately **off the trust-hashed profile surface**: materializing a
coordinator profile to host it would create a new trust anchor and arm the
security-deny floor on previously ungoverned root files. Its consumer is a
Stop-time advisory (never a deny) that flags an export the turn removed which a
sibling workspace still imports, confirmed by a live presence re-check on the
importer. TypeScript/JS cross-package resolution only; Python is a documented
gap.

---

## Hook stack

Six hook scripts are wired across six Claude Code events (PostToolUse is
registered twice; `stop-backstop` is reused for Stop and SubagentStop). All
registrations route through `run-hook.cmd <script>`, a polyglot wrapper that is
valid as both a Windows batch file and a Unix bash script.

| Event | Matcher | Script | Shell timeout |
|---|---|---|---|
| SessionStart | startup, resume, clear, compact | `session-start` | 3s |
| PreToolUse | Edit, Write, NotebookEdit | `preflight-and-advise` | 3s |
| PostToolUse | Bash, Edit, Write, NotebookEdit | `posttool-recorder` | 3s |
| PostToolUse | Edit, Write, NotebookEdit | `posttool-verify` | 3s |
| UserPromptSubmit | (all prompts) | `callout-detector` | 3s |
| Stop, SubagentStop | (all) | `stop-backstop` | 55s |

Every script shares one skeleton: a `CHAMELEON_DISABLE=1` short-circuit before
any work, plugin-root resolution, a log path computed in pure shell, interpreter
resolution, and a timeout-wrapped spawn of `python -m chameleon_mcp.hook_helper
<name>` with a fail-open trailer. The shell wrapper always exits 0; deny and
block decisions are emitted as JSON by `hook_helper`, never via the shell exit
code.

**Interpreter resolution** (`_resolve-python.sh`) is the fix for the macOS
silent fail-open. It walks a ladder, first match wins: the bundled dev venv,
then version-named interpreters (`python3.13/3.12/3.11`), then `uv run` against
the bundled project (which materializes a >=3.11 interpreter with chameleon's
deps), then an unversioned `python3`/`python` only after a probe confirms it is
>=3.11. macOS ships `/usr/bin/python3` as 3.9.x, below chameleon's floor and
without third-party deps; a blind fallback there would land every hook on an
interpreter that silently disables enforcement, so the unversioned rung is
gated. When nothing viable resolves, SessionStart emits a visible degraded
banner and the other hooks fail open silently and log a `no-interpreter` line.

**What each hook does:**

- **session-start** injects a compact ~3.6k-char operational digest of the
  `using-chameleon` skill (`_using_chameleon_digest`) instead of the full
  ~13.6k-char skill body, detects the repo and language, and injects the
  convention primer wrapped in `<chameleon-context>` (the
  `<chameleon-conventions>` block leads, BEFORE the digest, and carries
  explicit anti-majority framing — a rule buried after thousands of chars of
  mechanics measurably loses authority, and a model otherwise dismisses a
  taught rule that contradicts the sibling-file majority as "inverted";
  results-published/migration-ab-2026-07-11.md). The whole SessionStart
  emission (conventions, digest, banners, dead-session finding delivery) is
  packed under one `SESSION_START_DELIVERY_TOKEN_CEILING` budget with the
  digest as the only compressible part, so the higher-authority conventions
  block always stays inside the window a model actually follows. When the repo already imports
  `.chameleon/conventions.md` into the memory channel — detected
  delivery-faithfully: code fences/inline code spans are ignored and the
  import path must resolve (relative to its containing file) to an existing
  mirror — the block drops PER ITEM whatever line the mirror already
  delivers and keeps only what it doesn't (a missing whole section still
  injects in full; a bullet is dropped only when its exact line AND its
  section header are both in the mirror, so an out-of-section recital in a
  decoy mirror cannot suppress a current rule; a header stays attached to
  any surviving bullet under it), collapsing to a one-line pointer only when
  every line is already covered, instead of injecting everything twice; kill
  switch `CHAMELEON_MEMORY_CHANNEL_DEDUP=0`. It appends a drift banner when
  warranted, delivers any turn-end findings a prior dead session left
  undelivered (the next-turn delivery point when no next Stop arrives), adds a
  one-line note when the self-learning miner has proposed idiom candidates
  (`_idiom_candidates_note`), runs the default-on auto-refresh, and
  fires the advisor daemon asynchronously. It also wires the status line: when
  neither the project's `settings.json` nor the global `~/.claude/settings.json`
  declares a `statusLine`, `_wire_statusline_settings` writes the chameleon
  statusline command into the project's `settings.local.json`. A status line
  the user already configured is left alone, and any error leaves the settings
  untouched.
- **preflight-and-advise** primes the model before an edit. It resolves the
  archetype (daemon fast path, in-process fallback), applies the trust gate,
  runs the pre-write deny gates (secret, eval, banned import), and injects
  tiered context: a short pointer for an archetype already seen this session,
  or the full canonical excerpt on the first edit in an archetype or after a
  prior violation. (The drift observation for the edit is recorded once, by
  posttool-verify, which also covers denied and failed edits and the
  no-archetype branch; recording at preflight too doubled every drift
  statistic.) The witness is injected whole up to a 16,000-character cap with
  a truncation marker — quality over token cost; the 5 MB safe-read ceiling
  bounds the underlying file read, not the injection. The Tier-2 block carries
  several more default-on sections, each with its own kill switch:
  - **Nearby collaborator signatures** (`CHAMELEON_NEARBY_SIGNATURES=0`): the
    real callable signatures of source files in the edited file's directory,
    from the precomputed `symbol_signatures.json`, ranked by recorded call
    proximity from `calls_index.json` so the closest collaborators lead.
  - **Inbound-caller contracts** (`CHAMELEON_INBOUND_CALLERS=0`): the edited
    file's own exports with the recorded call sites that depend on each, plus
    a "change a signature -> update these call sites in the same turn"
    directive. The counterpart of the sibling signatures: outbound shows the
    contracts to call, inbound shows the dependents that break. It fires only
    when real caller edges exist, is bounded, and carries an honesty note
    (barrels and dynamic dispatch are invisible to the snapshot, so an empty
    list is not proof a break is safe).
  - **Archetype facts** (`CHAMELEON_ARCHETYPE_FACTS=0`): on a first-in-archetype
    edit, the archetype's class contract (base class, required methods, DSL
    macros) and its `key_exports` rendered as a "reuse these before creating a
    new one" directive, read from `conventions.json` and scoped to the edited
    archetype.
  - **Taught counterexample** (`CHAMELEON_COUNTEREXAMPLE=0`): when a taught
    competing import is still present in a real file, that off-pattern line is
    paired with the witness as a "do NOT write it this way" contrast, read
    from the precomputed `counterexamples.json`.
  The witness sits inside the imitate-spotlight; the counterexample, inbound
  contracts, and archetype facts render outside it (they are chameleon
  directives or negative examples, not data to imitate), with every value
  sanitized at the boundary.
- **posttool-recorder** records the HMAC-signed Bash exec log and re-lints
  single-target Bash file writes (`>`, `>>`, `tee`, `sed -i`) into the
  enforcement state so the Stop backstop covers them. The capture is
  deliberately narrow: only a single unquoted (or simply quoted) literal target
  word counts — globs, variables, command substitution, heredocs piped onward,
  and patch-body writes like `git apply` yield no target and are skipped, and
  the target must resolve inside a profiled repo. (The drift observation and
  the per-edit decision row are recorded by posttool-verify, not the recorder.)
- **posttool-verify** lints the written file against its archetype and emits
  violations as advisory context. Gated additionally by `CHAMELEON_VERIFY`.
- **callout-detector** (UserPromptSubmit) captures checkable intent tokens for
  the turn-end judge and surfaces disable/pause/teach options on detected
  frustration.
- **stop-backstop** runs the turn-end gates (see [Enforcement](#enforcement)).
  As of v3.4.0 it is deterministic and fast: it runs the inline deterministic
  advisories, decides whether the turn warrants a model review, and if so
  launches ONE detached background job and returns — it does not wait on the
  reviewer, so a model review never delays turn end. The 55s shell timeout
  (headroom under Claude Code's 60s hook ceiling) now bounds only a hung
  interpreter and the opt-in `CHAMELEON_JUDGE_WAIT=1` in-turn poll path, not an
  inline reviewer spawn.

**Fail-open and fail-closed split.** Every hook failure path fails open: a
degraded banner or nothing at all, and the edit proceeds. There is no
fail-closed PreToolUse path — an unreadable config, an unresolvable path, or a
crashed lookup emits an empty decision rather than a deny (failing closed on an
unreadable config would be circular), and the only PreToolUse denies are the
deliberate scans of the *proposed content*: the hard-kind secret deny, the
error-severity eval deny, and the banned-import deny. The genuinely fail-closed
pieces live elsewhere: the intent-capture hard-secret scan (a hit persists zero
tokens), the escape-hatch string-literal blanking (text it cannot lex safely
never activates a directive), and the merge driver (a merge it cannot run is
left conflicted for manual resolution, never silently resolved). An edit never
fails because chameleon's advisory layer broke.

Output uses the `additionalContext` channel, not `decision: block`, for
advisory feedback, so the model reads the context without re-prompt or
tool-retry loops. (The block gates do use `decision: block`, deliberately and
narrowly.)

---

## MCP server (chameleon-mcp)

FastMCP, stdio transport, server name `chameleon-mcp`, entry point
`chameleon_mcp.server:main`. It is launched by `.mcp.json` with
`uvx --refresh-package chameleon-mcp --from ${CLAUDE_PLUGIN_ROOT}/mcp chameleon-mcp`.
It is never exposed over a network.

Every tool is registered in `server.py` via the `_wire_tool` decorator and
delegates to `tools.py`. Every file-reading tool goes through `safe_open`
(lstat first, realpath, repo-boundary prefix match) and re-checks artifact
mtimes per call so a `/chameleon-teach` or `/chameleon-refresh` is picked up
without a stale cache. The server exposes **19 tools**: the 16
comprehension/conformance tools an agent needs mid-edit stay top-level, and
every operator/workflow function is folded behind three dispatchers
(`chameleon_lifecycle`, `chameleon_review`, `chameleon_telemetry`). A
dispatcher call is `chameleon_lifecycle(action="bootstrap_repo",
params={...})` — `action` names the folded function and `params` carries its
original arguments, names and values unchanged.

**Wire contract (v4.3).** Every registered tool sends its result as one
compact JSON text block: `structured_output=False` (no outputSchema, no
structuredContent duplication), no pretty-print indentation, and null-valued
fields dropped — absent == null for every documented field. The module-level
functions in `server.py` still return plain dicts (the decorator registers a
serializing wrapper and leaves the module symbol untouched), so hooks, the
daemon, and tests are unaffected. Token discipline follows the client's real
limits: Claude Code truncates each tool description and the server
instructions at 2KB, so every description fits under that (the dispatcher
docstrings carry one line per action; `action="help"` returns each action's
full signature + summary, generated from the live `tools.py` signatures via
`inspect`, so the reference can never drift from the code). The server sets
the FastMCP `instructions` field — the only server text guaranteed in model
context at session start under deferred tool loading — as a skill-style
"when to search for these tools" trigger plus the shared response
conventions. All 16 read tools and `chameleon_telemetry` carry
`readOnlyHint`/`idempotentHint` tool annotations; the two mutating
dispatchers deliberately do not. Row-heavy responses are shaped for token
economy at the source: `get_callers` groups one row per (path, caller,
grade, via) with call lines in `lines`; `get_blast_radius` chains start at the
first caller (the queried function is never repeated per chain);
`get_duplication_candidates` spends body excerpts from one global char
budget in rank order and names the rest with `excerpt_omitted: true`;
`describe_codebase` caps archetype rows (`DESCRIBE_MAX_ARCHETYPES`) and
reports `archetypes_omitted`. Truncation is always flagged, with a note
saying how to narrow the query.

### Kept top-level (16 tools)

| Tool | Purpose |
|---|---|
| `detect_repo` | repo_id, root, profile status, trust state for a path. |
| `get_archetype` | The archetype a file matches, plus alternatives and content-signal match. |
| `get_pattern_context` | Collapsed call: archetype + canonical + rules + idioms + meta in one round trip. |
| `get_canonical_excerpt` | The canonical witness source for an archetype. |
| `get_rules` | Repo-global rules keyed by source. |
| `lint_file` | Validate file content against an archetype; returns violations and confidence. |
| `search_codebase` | Find symbols by name or file from the committed index, ranked. Comprehension. |
| `describe_codebase` | Structural overview: language, framework, archetypes, totals, god symbols. Comprehension. |
| `get_callers` | Deterministic committed callers of a function. |
| `get_callees` | What a function calls (forward edges), inverting the reverse calls index. Comprehension. |
| `get_blast_radius` | Bounded transitive callers of a function (multi-hop change reach); the judge's own walk, surfaced as a tool. |
| `query_symbol_importers` | Importers of a module's exports plus which break on rename. TS/JS + Python; Ruby via the constant graph. |
| `get_crossfile_context` | Cross-file existence breaks (removed/renamed exports still imported). TS/JS + Python; Ruby via the constant graph. |
| `get_contract_breaks` | Deterministic caller-contract breaks for a diff: positional narrowings + removed-but-still-imported exports (`kind: "removed_export_still_imported"`). |
| `get_duplication_candidates` | Existing functions a file's new functions may re-implement. |
| `explain_edit` | Replay what chameleon knew and did at a file's last edit. |

### `chameleon_lifecycle` dispatcher

Profile lifecycle, teaching, trust, and opt-out — 13 actions.

| Action | Purpose |
|---|---|
| `bootstrap_repo` | First-time analysis and atomic profile commit (`force` overwrites). |
| `refresh_repo` | Re-analyze, detect drift, update the profile (flock-locked). |
| `trust_profile` | Mark a committed profile trusted (requires a confirmation token). |
| `list_profiles` | Cursor-paginated list of every repo this user has touched. |
| `merge_profiles` | Three-way profile merge (re-cluster from the union); used by the merge driver. |
| `teach_profile` | Apply a free-form correction (idiom, banned import, wrapper). |
| `teach_profile_structured` | Structured idiom capture (slug, rationale, example, counterexample, archetype, status, and a `source` provenance line). |
| `teach_competing_import` | Capture a wrapper preference ("use X, not Y"). |
| `unteach_competing_import` | Remove a taught wrapper preference. |
| `propose_archetype_renames` | Suggest better names for the largest archetypes. |
| `apply_archetype_renames` | Atomically apply a rename mapping. |
| `disable_session` | Suppress injections for a session (HMAC-signed marker). |
| `pause_session` | Pause injections for N minutes (default 15; HMAC-signed marker). |

### `chameleon_review` dispatcher

The review-gate workflow — 7 actions. All read-only except
`record_review_verdict` / `record_finding_fate` (ledger appends) and
`dep_audit` (spawns the user's own auditor, gated).

| Action | Purpose |
|---|---|
| `get_autopass_verdict` | Advisory: is a branch diff safe to auto-pass, or needs human? Never gates. |
| `refute_finding` | Round-3: independently refute review findings via spawned no-tools refuters. |
| `record_review_verdict` | Append an HMAC-signed pr-review verdict to the ledger. |
| `record_finding_fate` | Append a per-finding disposition (accepted/declined/converted) to the fate ledger. |
| `get_review_history` | Recent ledger verdicts, newest first, HMAC-verified. |
| `scan_dependency_changes` | No-network supply-chain review of a manifest/lockfile diff. |
| `dep_audit` | Opt-in `npm audit`/`bundler-audit`; the only network action. |

### `chameleon_telemetry` dispatcher

Observability and health — 14 actions, all read-only.

| Action | Purpose |
|---|---|
| `get_status` | Enforcement mode plus active and demoted block rules. |
| `get_drift_status` | Freshness, days since refresh, drift score, production-ref block. |
| `get_drift_antipatterns` | Recurring-violation signals from drift history; drives auto-idiom. |
| `get_shadow_report` | Per-rule would-block counts for the shadow-to-enforce decision. |
| `get_override_audit` | Per-rule inline-override rate and blanket share. |
| `get_longitudinal_signals` | Structural-conformance and enforcement-outcome tracks, kept separate. |
| `get_finding_fate_stats` | Per-lens precision (accepted vs declined) from the finding-fate ledger. |
| `get_shelved_findings` | Below-surface-bar findings currently shelved, for `/chameleon-status`/`/chameleon-explain` browsing. |
| `get_idiom_coverage` | Read-only map of guidance already captured. |
| `check_idiom_candidates` | Novelty gate (novel/duplicate/covered/invalid) before teaching. |
| `list_idiom_candidates` | Unapproved idiom proposals the self-learning miner derived from usage; nothing here is adopted. |
| `get_prose_rule_candidates` | Doc-stated "use X not Y" rules, corroborated against the repo's imports. Propose-only. |
| `daemon_status` | Advisor daemon liveness and version. |
| `doctor` | Installation health triage. |

---

## Enforcement

Most feedback is advisory and shapes the code without ever blocking. A narrow
gate stack lets a small calibrated set of high-confidence violations actually
block, gated so a block fires only where it will not produce false positives.

### Modes

From `config.json` `enforcement.mode`, validated against `{off, shadow, enforce}`,
**default `enforce`**:

- **off** advisory only; no block point fires.
- **shadow** every gate computes its decision and logs a `would_block`
  metric, but the edit or turn proceeds. Use it to measure a repo's
  false-positive rate before turning on real blocks.
- **enforce** (default) the gates block for real. Every block needs a trusted
  profile and is overridable inline. The guard differs by class: the per-edit
  convention denies (naming/import/jsx/file-naming) require per-repo
  zero-false-positive calibration against the repo's own committed files plus a
  high- or medium-confidence archetype match; the archetype-independent security
  facts (hard-kind secrets, eval/exec sinks) block on deterministic detection
  with no confidence gate. So enforce is the safe default for the calibrated
  convention rules without a measure-first shadow period, not a blanket
  "every block is calibrated" guarantee.

`CHAMELEON_ENFORCE=0` forces advisory-only for the whole session regardless of
mode. `/chameleon-disable` and `/chameleon-pause-15m` suppress all behavior for
their window, including enforce-mode blocks.

### Block points

Four places can stop work, all gated by trust, mode, and `CHAMELEON_ENFORCE`.
(A fifth used to exist here: a once-per-session Stop-hook idiom-review
interrupt. As of v3.4.0 idiom review is advisory-only — one lens of the
async-first turn-end review job described in
[Turn-end advisories](#turn-end-advisories-on-by-default-never-block) — so it
no longer stops work.)

1. **PreToolUse secret deny.** A deterministic hard-kind credential in the
   *proposed* content. Archetype-independent (fires even when no archetype
   resolves), gated on a trusted profile and enforce mode — the rule itself is
   calibration-EXEMPT (always in the active set; calibration runs no content
   scans, so a witness-count floor must not disarm it) — scanning the first
   100 KB with a regex-only hard-kind scanner so the hot path holds on large
   payloads. The eval/exec deny shares the same exemption.
2. **PreToolUse import deny.** A banned or competing import in the proposed
   content, gated on a confident archetype match: `match_quality` in
   (`ast`, `exact`) at `high` or `medium` confidence. `exact` is the path-based
   match a brand-new file resolves to (a Write target with no content on disk
   yet) — a stronger signal, not a weaker one, and excluding it would let every
   new file slip the deny exactly where a banned import is most often
   introduced.
3. **PostToolUse block.** A hard-class violation on a file already escalated to
   L2, when the archetype match is AST-grade at high or medium confidence and
   the profile is trusted and not stale. `phantom-import` is deferred from here
   to the Stop backstop.
4. **Stop backstop.** At turn end, a file with an unresolved hard-class
   violation that still fails a live re-lint refuses to end the turn, bounded by
   `enforcement.stop_block_cap` (default 3). This is the only place
   `phantom-import` and a single-edit secret block, since they are
   level-independent here. **Multi-root** (default on, kill switch
   `CHAMELEON_MULTIROOT_STOP=0`): the per-edit hooks key enforcement state by
   each edited file's OWN workspace repo_id, not the launch cwd, so at a
   pure-coordinator monorepo root the Stop discovers every touched workspace
   (`_discover_stop_roots` globs the session's state files and regroups their
   recorded files by `find_repo_root(file)`) and runs the gate pipeline per
   workspace against its own profile. Per-workspace trust is honored and never
   unioned (`grants_root`), at most one reviewer spawn is paid across the whole
   Stop, the block short-circuits on the first blocking root (so the cap is
   charged to one root per Stop), advisories from every non-blocking workspace
   merge into one context, and one attestation is written per distinct run-root.
   A single-repo session is output-equivalent to the legacy single-root path.
   Closes the v2.38.28 coordinator-root turn-end dead spot.
### Escalation

Per-file escalation, invisible to the user, stored in a session-scoped
enforcement-state file. Levels: NONE -> L0 (silent fix) -> L1 (flagged) -> L2
(stop and fix). A re-violation within a 10-second self-correction window does
not escalate; a clean pass de-escalates one level. A 30-second cooldown applies
to a file with no recorded violation (5 seconds once escalated); within it,
re-edits get `[🦎 chameleon: already verified this file]`.
`MAX_CORRECTIONS_PER_FILE = 10` stops verifying a file caught in a tight
verify-edit loop, and `stop_block_cap` bounds repeated Stop blocks per session.

### Calibration

A block-eligible rule becomes active for a repo only after it flags near-zero of
the repo's own committed files. At bootstrap and refresh,
`enforcement_calibration.py` samples each archetype witness plus a bounded set
of same-extension siblings (caps: 1200 files, 20 siblings each), runs the real
lints, and computes `fp_rate = flagged_files / sampled`. A rule is active only
when `fp_rate <= 0.0005` over a positive sample; with the default cap, a single
flagged file already exceeds tolerance, so this is effectively zero-FP. A rule
with no signal source for the profile's language stays inert (so a vacuous 0.0
cannot certify it), and `naming-convention-violation` additionally needs a
convention at >=0.60 consistency. The verdict is written to `enforcement.json`,
and `get_status` reports the active set plus each demoted rule with its measured
`fp_rate`. An override-feedback pass at refresh auto-demotes a calibrated rule
the team keeps overriding (below a session floor, or for the security rules, it
is only proposed, never auto-demoted).

The block-eligible rule set is `phantom-import`, `import-preference-violation`,
`jsx-presence-mismatch`, `naming-convention-violation`,
`inheritance-convention-violation`, `file-naming-convention-violation`,
`secret-detected-in-content`, and `eval-call`. The last two are
security-deterministic and stay active by default rather than by measurement
(calibration does not run content scans). They are blanket-immune: see the
escape hatch.

### Rule catalog

| Rule | Severity | Class |
|---|---|---|
| `secret-detected-in-content` | error | Hard for deterministic credential kinds; blanket-immune. |
| `eval-call` | error / warning | Hard at error (direct eval); blanket-immune. |
| `phantom-import` | warning | Deterministic, archetype-independent; blocks only at Stop. |
| `import-preference-violation` | warning | Archetype-dependent, calibration-gated. |
| `jsx-presence-mismatch` | error / warning | Block-eligible only at error. |
| `naming-convention-violation` | warning | Calibration- and signal-gated. |
| `inheritance-convention-violation` | warning | Ruby and Python; calibration-gated. |
| `file-naming-convention-violation` | warning | Calibration-gated. |
| `default-export-kind-mismatch`, `top-level-node-kinds-mismatch`, `named-export-count-bucket-mismatch`, `content-signal-mismatch` | warning/info | Advisory structural shape. |
| `insecure-random`, `weak-hash`, `sql-string-interpolation`, `command-injection`, `insecure-deserialization` | warning | Advisory security sinks; never block-eligible. |
| `style-rule-violation`, `then-without-catch`, `required-guard-convention` | warning/info | Advisory. |
| `skipped-test`, `tautological-assertion`, `real-sleep-in-test`, `random-in-test`, `assertion-free-test`, `unstubbed-network`, `unfrozen-clock` | info | Advisory, test archetypes only. |
| `phantom-symbol`, `cross-file-importers`, `removed-export-breaks-importers` | info/warning | Advisory cross-file. |

### Escape hatch

A block is overridable inline with `// chameleon-ignore <rule>`
(`# chameleon-ignore <rule>` in Ruby, `/* chameleon-ignore <rule> */` for a TS
block comment). A bare `// chameleon-ignore` (no rule) suppresses every block on
that line; `// chameleon-ignore-file <rule>` covers the whole file. A trailing
directive covers its own line; a directive on its own line covers that line and
the one below. The directive must end its line, so prose that merely mentions
one never activates it, and directives inside string literals are blanked before
the scan so attacker-controllable text cannot switch a rule off.

Line/line-below scoping requires the violation to report a line in the first
place. Four rules never do — `import-preference-violation`,
`naming-convention-violation`, `inheritance-convention-violation`, and
`file-naming-convention-violation` — because their checks report a repo-wide or
whole-file fact (a banned module used anywhere, an identifier's casing, a
file's own name) rather than a single flagged line. A plain directive naming
one of these falls back to file scope: it suppresses every occurrence of that
rule in the file, the same as `chameleon-ignore-file`, not only the occurrence
nearest the directive. This is what the PreToolUse import-preference deny, the
`lint_conventions` naming/inheritance/file-naming checks, and the turn-end
opt-out checks all read via the flat `ignored_rules()` view in
`violation_class.py`, since those gates have no per-line granularity to filter
against. The line-bearing rules above (secrets, `eval-call`) are unaffected —
their violations carry a real line, so `is_violation_ignored` scopes a plain
directive to it as described.

The deterministic hard class is the exception: a hard-kind secret and an
error-severity `eval-call` are never covered by the bare form. Suppressing one
requires the rule name (`// chameleon-ignore secret-detected-in-content`,
`// chameleon-ignore eval-call`), keeping a security bypass deliberate and
auditable. Advisory-grade variants (entropy-based secret hits, warning-severity
dynamic eval) stay bare-suppressible.

The directive is scoped to human-approved exceptions. The deny reasons and the
using-chameleon skill both instruct the model that it must not add an ignore on
its own judgment — in particular never because existing files still use the
blocked pattern (they may be mid-migration; the block encodes the team's
current decision). Measured motive: in the migration-scenario A/B the deny's
old "add the directive if intentional" phrasing was used by the model to
self-approve keeping the majority's wrong import; scoping the directive to
human approval eliminated every wrong-import completion
(results-published/migration-ab-2026-07-11.md).

Every inline override records one durable row in `drift.db.rule_overrides` (with
a `blanket` flag for bare directives). `get_override_audit` and
`/chameleon-status` surface the per-rule override rate so a rule fighting the
team stays visible. The override audit never auto-mutates the trust-hashed
`enforcement.json`; a recalibration runs only at refresh.

### Turn-end advisories (on by default, never block)

As of v3.4.0 the model-reviewed advisories are async-first (spec "Stop hook +
idiom system overhaul" section 3.1): the Stop hook's deterministic gates
decide whether a turn warrants a review and, if so, launch exactly ONE
detached background job (`stop/scheduler.py` owns every spawn decision;
POSIX `start_new_session=True`, Windows `CREATE_NEW_PROCESS_GROUP |
DETACHED_PROCESS`) and return immediately — Stop never waits on it, so a
model review never delays turn end. This replaces the pre-3.4.0 design (a
synchronous per-turn spawn, an opt-in `CHAMELEON_JUDGE_ASYNC=1` detached
variant, and a "multi-lens" config toggle) with one pipeline that is always
detached. `CHAMELEON_JUDGE_WAIT=1` is the one exception: it makes Stop poll
the same job and render in-turn, for one-shot harness/eval sessions with no
next turn to deliver into (see the `environment-variables.md` entry).

- **The review job** (`stop/job.py`, spawned by `stop/scheduler.py`) runs the
  turn's active lenses under one `core.budget.TurnBudget`
  (`JOB_TOTAL_BUDGET_SECONDS`, default 240s), each producing canonical
  `core.finding.Finding` objects (`stop/lenses/__init__.py`'s `LensResult`):
  - **Correctness** (`enforcement.correctness_judge`, default on,
    `stop/lenses/correctness.py`). A `claude -p` reviewer reads the turn's
    reconstructed diffs for logic errors the static engine cannot see:
    unguarded optional derefs, dropped awaits, off-by-one, inverted
    conditions, dead code. The prompt is fed on stdin (never argv), all tools
    are disallowed, the child runs with `CHAMELEON_DISABLE=1` to prevent
    recursion, secret-bearing files are filtered out, and diff bytes/file
    count/finding count are capped. Grounded with committed caller facts,
    multi-hop transitive callers, imported-symbol signatures, and captured
    intent tokens (each grounding block has its own default-on config flag,
    and each fails open independently — an absent calls index or a per-file
    parse exception inside one grounding stage skips just that stage's
    contribution and still lets the review spawn and return findings). As of
    v3.6.0 it also carries a **task intent contract**: the verbatim,
    secret-scanned scope-constraint sentences the session's own prompts stated
    (`intent_capture.extract_scope_lines`, kill switch
    `CHAMELEON_INTENT_CONTRACT=0`), against which the lens runs two
    `kind:intent` checks — unmet-ask (a requested change with no implementation
    in the diff) and unrequested-scope (a change the prompt did not ask for) —
    routed to the standard finding lifecycle by the reviewer's own `claim_type`
    (`stop/lenses/correctness.py::_kind_for`). Model:
    `CHAMELEON_JUDGE_MODEL` (default `sonnet`). The **reviewer model ladder**
    (`judge_model_for_route`, `CHAMELEON_JUDGE_MODEL_HIGH`, default `opus`)
    escalates a `risk_high`/`intent_forced` route to a stronger model;
    `risk_elevated`/`first_low_risk` keep the base model. Unconditional since
    every review now runs inside the detached job (no separate synchronous
    path left to protect from a slower model, unlike the pre-3.4.0 ladder) —
    `CHAMELEON_JUDGE_TIERING=0` flattens every route back to the base model.
    See the `CHAMELEON_JUDGE_MODEL_HIGH` entry in `environment-variables.md`.
  - **Duplication** (`enforcement.duplication_review`, default on,
    `stop/lenses/duplication.py`). Each new function is matched by body hash
    against the committed function catalog and functions added earlier this
    session; a hit goes through a bounded judge spawn that confirms real
    re-implementations. Model independently tunable with `CHAMELEON_DUP_MODEL`.
  - **Idiom** (`enforcement.idiom_review`, default on, `stop/lenses/idiom.py`).
    Scoped to the edited archetypes'/languages' idioms, cited by diff hunk; a
    violation claim must carry the idiom's slug and the offending lines or it
    is dropped. Replaces the pre-3.4.0 once-per-session Stop-hook self-review
    INTERRUPT (`_idiom_review_gate`, deleted) — idiom review is advisory-only
    now, like every other lens, and a compliant turn is silent. Tuned with
    `CHAMELEON_IDIOM_LENS_MAX_IDIOMS`/`_MAX_PROMPT_BYTES`/`_MAX_FINDINGS`.

  Every finding from every lens then passes through **VERIFY**
  (`stop/verify.py`, `CHAMELEON_STOP_VERIFY=0` to disable): the independent
  refuter (`refuter.py`) checks each single-file-local finding (`correctness`
  or `idiom` kind — a `duplication` finding's two-location evidence is exempt
  and passes through pre-confirmed) before it can surface; a refuted finding
  is DROPPED, a confirmed one is tagged `[confirmed]`, everything else is
  `[unverified]`. On any VERIFY failure every finding passes through
  unverified, never dropped. The job persists survivors to the finding ledger
  (below), pre-renders a delivery payload, and exits — always 0, fail-open at
  every stage (a stage exception is a check event, never a crash).

- **Deterministic advisories** (each a default-on config flag, run inline in
  the Stop hook itself, not part of the detached job): stale-test (a
  changed source whose paired test is untouched), change-set completeness (a new
  file of a kind that needs a companion, like a Rails model needing a
  migration), cross-file existence break, the cross-workspace existence break
  (an export the turn removed that a sibling monorepo workspace still imports,
  read from the coordinator `cross_reverse_index.json` and confirmed by a live
  re-check; kill switch `CHAMELEON_CROSSWS_INDEX=0`, see
  [Cross-file indexes](#cross-file-indexes)), test integrity (live source
  changed while tests were weakened), and intent scope drift (changed files
  that share no word with any requested identifier).
- **Finding lifecycle ledger** (`review_ledger.py`, one JSON row per
  `match_key` under the repo's plugin-data dir). `stop/job.py` persists every
  VERIFY survivor via `record_findings`; a finding below the per-repo
  **surface bar** (`review.surface_bar`, default `medium`: `medium` surfaces
  blocker/high/medium even unverified and low only when
  `verified=="confirmed"`; `high` raises the floor; `low` surfaces everything)
  is stored `status="shelved"` instead, with a check event. A shelved finding
  whose `match_key` recurs across sessions is auto-promoted to `pending` on
  its second sighting (`SHELVED_PROMOTE_MIN_RECURRENCE`, kill switch
  `CHAMELEON_SHELVED_PROMOTION=0`) — a below-bar finding a team keeps
  re-triggering is worth a human look. Shelved rows are browsable via
  `get_shelved_findings` (`/chameleon-status`, `/chameleon-explain`).
  Delivery reads `undelivered_findings` (`pending` rows only) and
  marks rows `delivered`/`addressed` as they are shown or re-addressed (the
  cited file changed since review). Separately, at the START of the next
  Stop (`CHAMELEON_FINDING_LEDGER=0` to disable), before that turn's own
  gates persist anything, `compute_resurface` re-checks every open row and
  surfaces an unaddressed HIGH/blocker finding exactly once via a
  `<chameleon-context>` block folded into the ranked Stop assembler
  (`stop/assemble.py`'s `assemble_stop_context`). `compute_resurface` is
  deliberately split from the terminal commit: it never writes `resurfaced`
  itself, only reporting the candidate rows' match_keys, because a multi-root
  Stop's caller (`hook_helper.stop_backstop`) must not spend a row's one-shot
  resurface on a turn whose output a LATER workspace's block ends up
  discarding. `stop_backstop` calls `mark_resurfaced` once, after its whole
  multi-root loop confirms no root blocked, for exactly the match_keys that
  survived both the loop (not orphaned by a later block) and the ranked
  packer's ceiling (not dropped for space) — `resurfaced` is a TERMINAL status
  `undelivered_findings` never returns again, so the one-shot resurface line
  is the finding's sole re-appearance rather than looping back through
  ordinary delivery and re-arming itself. A stale finding (digest changed
  since creation) renders `[stale]` rather than being dropped — the pre-3.4.0
  silent-drop behavior this replaced is gone by design. This superseded the
  older `judge_findings` table in drift.db (`_ledger_persist`/
  `_ledger_recheck_and_resurface` in `stop/gates.py`, `record_judge_finding`/
  `open_judge_findings`/`mark_judge_finding` in `drift/observations.py`),
  which the async-first cutover left wired to nothing; that store and its
  functions have been retired. Any HIGH finding it was still holding open
  from before the cutover is not migrated (a documented, low-impact gap —
  unlike the `.judge_pending.<session>.json` queue, which IS migrated via
  `migrate_pending_queue`).

### Stop assembly and token budgets

As of v3.5.0 every turn-end emission is packed by one ranked, token-budgeted
packer, `stop/assemble.py::assemble_stop_context`, instead of concatenating
independently-capped blocks. Candidates arrive as `EmissionItem`s carrying a
priority and the finding `match_keys` they represent. The ranked order (the
`PRIORITY_*` constants, lower packs first) is: block reason > resurfaced HIGH >
delivered-verified > delivered-unverified > deterministic advisories >
idiom/nudge lines. Packing is greedy and **whole-item** under
`STOP_RENDER_TOKEN_CEILING`: an item that does not fit whole is omitted, never
truncated mid-item, so it stays reachable at the next delivery point. A present
block item short-circuits everything — its text is emitted as-is and nothing
else packs alongside it ("a hard block emits only the block reason").

The packer returns `AssembledStop.packed_match_keys`: the union of the packed
items' keys, exactly the findings that reached the emitted text. This is the
**ranked-commit invariant** — the caller commits a `delivered`/`resurfaced`
terminal transition only for `packed_match_keys`, never for a finding the
ceiling dropped or a block discarded (a block-present emission returns an empty
tuple, since a block delivers nothing). It is why `compute_resurface` is split
from `mark_resurfaced`: a HIGH finding's one-shot resurface commits only after
the whole multi-root loop confirms no root blocked AND the packer kept the
line, so a resurface is never spent on output a later workspace's block or the
ceiling discards.

SessionStart uses the same budgeting idea: its ~3.6k-char operational digest is
the only compressible part of an emission bounded by
`SESSION_START_DELIVERY_TOKEN_CEILING`, so the higher-authority conventions
block stays inside the model's attention window.

### Self-learning idiom miner

As of v3.6.0 a miner (`stop/miner.py::run_miner`, kill switch
`CHAMELEON_IDIOM_MINER=0`) runs at the **tail of the detached review job**, so
it adds zero Stop-time cost. It reads three usage signals the rest of Stop
already writes — recurring correctness/duplication findings, over-overridden
rules, and reinforced idioms — and writes never-auto-adopted proposals to
`.chameleon/idiom-candidates/<slug>.json` (`core/idiom_candidates.py`, atomic
per file, merging repeated sightings into one richer proposal rather than
forking duplicates; new slugs bounded by `IDIOM_CANDIDATE_MAX`).

Nothing here is adopted automatically. The candidates directory is deliberately
**not** part of the trust-hashed profile surface (`_HASHED_ARTIFACTS`) —
hashing the miner's own unreviewed output would arm the trust gate on a
proposal no human has seen. A candidate becomes a real idiom only through the
same `/chameleon-teach` (or `/chameleon-auto-idiom`) path and the same
`check_idiom_candidates` novelty gate a hand-taught idiom passes. Candidates
surface non-intrusively: a one-line SessionStart note and a "Learned from
usage" section in `/chameleon-auto-idiom` (`list_idiom_candidates`).

---

## The review gate and trust path

The enforcement spine is the machine review gate. This section covers the
evidence surfaces that let a team trust it and the staged rollout that earns
that trust. The standing rule across every stage: any change touching the
"What stays human" classes, an unsupported language, or a file class the engine
does not verify goes to a human regardless of gate color.

### Staged rollout

- **Stage 0, bootstrap and trust.** Run `/chameleon-init`, review
  `profile.summary.md`, run `/chameleon-trust`. The default mode is `enforce`, so
  calibrated block rules are live once trust is granted.
- **Stage 1, shadow (optional).** A team that wants to measure before any edit is
  denied can set `enforcement.mode` to `shadow` first and leave it for two to
  three weeks of real editing. The lead reads `get_shadow_report` /
  `/chameleon-status --shadow`: per-rule would-block counts over a non-truncated
  window, distinct files and sessions, and a sampled file:line list to eyeball.
  Promotion is a human read of the sample, not a computed FP fraction.
- **Stage 2, enforce.** Set `enforcement.mode` back to `enforce` (the default).
  Trust persists across the config edit, so it takes effect immediately; only
  under `CHAMELEON_TRUST_REVALIDATE=1` does the trust-hashed edit require a
  re-grant. The lead watches the override-rate panel
  (`get_override_audit`): a rule overridden in a large fraction of edits is
  fighting the team, so either the convention is wrong (refresh/teach) or the
  rule is miscalibrated.
- **Stage 3, review-optional.** Wire the gate into CI: a change is merge-eligible
  when enforce mode passes clean (or blocks are individually overridden) and
  `/chameleon-pr-review` returns APPROVE or APPROVE WITH NITS, for changes inside
  the supported envelope. The lead watches the review ledger
  (`get_review_history`) for any merged-despite-BLOCK, the longitudinal panel,
  and the recovery loop (`explain_edit`).

### Evidence surfaces

- **Shadow report** (`get_shadow_report`) aggregates `metrics.jsonl` including
  rotated segments, flags a truncated window rather than asserting "0
  would-blocks" covers the period, and reports only frequency plus a sampled
  list, never an invented FP fraction.
- **Override audit** (`get_override_audit`) reads the durable `rule_overrides`
  table and surfaces contention, never auto-mutating a trust-hashed artifact.
- **Longitudinal signals** (`get_longitudinal_signals`) keeps two tracks
  separate: a structural-conformance track labeled "not a quality bar" with an
  explicit "does NOT cover logic, dataflow, cross-file, auth" line above any
  green panel, and an enforcement-outcome track.
- **Review ledger** (`record_review_verdict` / `get_review_history`) is an
  append-only, HMAC-signed NDJSON log. Each record pins the commit SHA, profile
  sha256 plus generation plus schema version, trust state, verdict,
  findings-by-severity, engine version, and reviewing user. It is
  **tamper-evident, not forgery-proof**: the HMAC key is the same per-user local
  key the gated developer holds, so it detects a third local user's edit but is
  not a CI merge gate. A real hard gate needs a server-side platform status
  check.
- **Recovery loop** (`explain_edit`) reads the durable `decision_log` table (not
  the refresh-wiped drift observations) and classifies a miss as a coverage gap
  (no archetype, or a fallback match) versus an in-scope miss, routing it to
  teach/refresh/a new rule.
- **Session attestation** (`CHAMELEON_ATTESTATION`, default on) writes one
  signed record per top-level Stop capturing which checks ran, skipped, or
  degraded, governed-versus-ungoverned touched files, inline overrides, and any
  disable window. It is **raise-only**: nothing in it may lower scrutiny
  anywhere downstream. A consumer may use it only to raise gate depth and to
  make post-incident replay honest; the merge gate's floor is computed from diff
  facts alone, so a forged-clean attestation buys nothing. The concrete
  consumer is the auto-pass router (`CHAMELEON_AUTOPASS_ATTESTATION=0` to
  disable; default on): `get_autopass_verdict` reads the recent attestations
  (window `CHAMELEON_ATTESTATION_MATCH_LIMIT`, default 25), attributes them to
  the branch diff by file overlap (`session_coverage_from_attestations`), and
  folds three governance signals in raise-only fashion — verification
  suppressed while the diff was written, a genuinely degraded judge spawn
  (never the deliberate low-risk skip), and an inline override on one of the
  diff's own files — each adding a needs-human reason, so an under-governed
  diff routes to a human on terms a fully-governed one does not. No match
  leaves the verdict identical to having no attestation at all, and the router
  still never blocks.

### PR-review and refuter

`/chameleon-pr-review` runs whether or not a ticket is supplied (the
logic-intent pass is the only part needing one). Every finding stays under the
integrity rule: a finding the engine cannot witness is dropped, and BLOCK/FIX
logic findings are hard-gated to anchor lines inside an added or changed hunk so
a pre-existing issue is never reported as PR-introduced. The passes cover
convention review, hunk-aware delta, no-network supply-chain
(`scan_dependency_changes`), security, Rails migration safety, co-change,
cross-file existence and contract breaks, and the ticket-gated logic trace.
Surviving model-judgment findings pass a round-3 refuter (`refute_finding`,
`CHAMELEON_REVIEW_REFUTER` default on, `CHAMELEON_REFUTER_MODEL` default
`sonnet`) that spawns an independent no-tools model per finding to try to refute
it; a finding it cannot ground is dropped. A "confirmed" verdict never
authorizes an edit or a post. BLOCK is reached two ways: by the witnessed
deterministic facts — a hard-kind secret inside an added or changed hunk, an
error-severity eval-call on an added line, an irreversible operation in a
Rails `change` migration — and by the model-judgment logic classes the hunk
gate admits: a removed guard or error branch that can crash or skip
authorization (the change-delta pass), a race condition, and a ticket
requirement with no implementation in the diff. The advisory passes
(authz/taint heuristics, migration table-size reminders, every cross-file
finding) cap at FIX. A
new direct dependency is an **ACK** — a confirm-before-merge line in its own
non-verdict channel — never a BLOCK, so a routine dependency add does not
pollute the durable verdict ledger. The skills draft replies and never
auto-post.

---

## Trust model

Trust is per-user and per-repo. The profile lives in the repo; the trust grant
lives on your machine at `<data>/<repo_id>/.trust` (a `TrustRecord` with
`granted_at`, `granted_by_user`, `profile_sha256`, `repo_root`, and a per-root
hash map for monorepo workspaces).

`detect_repo` computes one of three states:

- **untrusted** no grant, or the grant does not cover this root. No canonical
  injection; a trust prompt fires once per session. Edits proceed without
  guidance until trust is granted.
- **stale** the grant covers the root but the profile changed since (the granted
  hash no longer matches). Content injects with a warning that already suggests
  `/chameleon-trust`. **This state only occurs under `CHAMELEON_TRUST_REVALIDATE=1`**:
  by default trust persists across profile changes and never goes stale (see
  "Trust persistence" below).
- **trusted** the grant covers the root. Content injects normally. By default
  this holds across every later profile change; under the kill switch it also
  requires the hash to still match the grant.

The hash (`hash_profile`) is a SHA-256 over a fixed set of **17 artifacts**,
each framed by null bytes: `.archetype_renames.json`,
`archetypes.json`, `calls_index.json`, `canonicals.json`, `config.json`,
`constant_index.json`, `conventions.json`, `counterexamples.json`,
`enforcement.json`, `exports_index.json`, `function_catalog.json`, `principles.md`,
`idioms.md`, `profile.json`, `reverse_index.json`, `rules.json`, and
`symbol_signatures.json`. Once the per-file idiom store `.chameleon/idioms/`
exists, it supersedes `idioms.md` in the hash: `idioms.md` is dropped (it is a
generated view) and each `idioms/*.json` record is hashed in its place, sorted
by filename. The `idiom-candidates/` directory is deliberately excluded — it is
the miner's unreviewed output, not idiom truth. Because all
the convention, index, and calibration artifacts are in the set, a refresh that
changes any of them flips the profile to stale until re-approval — but only
under `CHAMELEON_TRUST_REVALIDATE=1`; by default trust is one-time and the
hash serves as provenance, not a staleness gate (see "Trust persistence"
below). `config.json`
is in the hash deliberately: it is why the override audit never rewrites
`enforcement.json` at runtime (that would silently flip the profile to stale and
disable blocking).

`trust_profile` requires a confirmation token equal to the repo basename or the
literal `yes-trust-<repo_id[:8]>`. On grant it re-scans `idioms.md` and
`principles.md` for injection and refuses if either looks suspicious. The write
is flock-serialized and atomic. `trust.auto_preserve_when` only controls whether
a refresh re-stamps the stored grant hash; it does NOT control re-prompting (the
staleness gate is `CHAMELEON_TRUST_REVALIDATE`, default off, see Trust
persistence below). By default trust is one-time and survives later profile
changes, including another user's committed change, so it does not re-prompt;
re-prompting on change happens only under `CHAMELEON_TRUST_REVALIDATE=1`.

### Trust persistence (default)

Trust is **one-time** by default: once a repo is trusted, the grant holds across
every later profile change (refresh, re-bootstrap, teach) and never goes stale,
so the user is never re-prompted to re-trust their own repo. Every staleness
decision funnels through one predicate, `profile_diverged_from_grant`, which
returns `False` unless `CHAMELEON_TRUST_REVALIDATE=1` is set; `is_material_change`
and the three inline hook gates (statusline, the PreToolUse enforcement gate, the
Stop gate) all route through it, so the policy is enforced in one place and read
at call time. Setting the kill switch restores the legacy behavior where any
hash change re-prompts.

Two consequences of persistence are deliberate:

- **Enforcement now applies to post-grant rule changes.** A profile whose
  `enforcement.json` / `config.json` changed after the grant is `trusted`, not
  `stale`, so calibrated block rules apply (and a `config.json` flip to
  `enforcement.mode: "enforce"` takes effect) where they previously fell through to
  advisory-under-stale. Intended, and bounded: only `BLOCK_ELIGIBLE_RULES` can be
  promoted (an arbitrary rule can't be planted), a promoted MEASURED rule blocks
  only on the calibrated verdict in `enforcement.json` (recomputed locally at
  every bootstrap/refresh; a pulled artifact carries the author's verdict until
  the next local refresh) — the two calibration-exempt security rules are active
  regardless of that artifact, closing the inverse tamper vector (a planted
  zero-witness or torn artifact can no longer disarm the credential/eval deny)
  — and the block reason is sanitized, so the worst case is a denied
  edit from a drifted/pulled profile, not code execution. A repo whose
  `enforcement.json` / `config.json` may change via an
  un-reviewed `git pull` and which wants those changes to re-prompt before they
  enforce should set `CHAMELEON_TRUST_REVALIDATE=1`.
- **Post-grant profile edits are no longer re-reviewed by the staleness gate.**
  The grant-time injection scan (`grant_trust` scans `idioms.md` / `principles.md`)
  only runs at the original grant, and render-site sanitization does **not**
  neutralize injection prose. So the injection defense is decoupled from staleness
  and moved to the prose READ path: `loader._prose_injection_unsafe` scans
  `idioms.md` at load (mtime-cached) and the SessionStart read scans
  `principles.md`, dropping the whole artifact (with a stderr warning) if it trips
  the same narrow injection / secret / dangerous-pattern scan. A poisoned prose
  artifact introduced by a manual edit, a malicious pull, or a teach whose
  re-grant was refused is therefore never served at full trust, even though trust
  persists.

What untrusted suppresses, at the data layer: the canonical excerpt body is
redacted, `get_rules` returns nothing, and the cross-file, callers, importers,
duplication, and refuter tools all return an untrusted status.
(`propose_archetype_renames` carries no trust gate: it reads only the profile's
own archetype names and paths, never witness content.)

---

## Atomicity, locking, and crash safety

`.chameleon/` is multi-process shared mutable state. The engine treats it as
such.

**Atomic multi-file commit.** Bootstrap and refresh write every artifact to a
transaction directory `<parent>/.chameleon.tmp/<txn-id>/` (txn id =
`<pid>-<uuid8>-<epoch>`). On clean exit they carry forward non-protocol
siblings, fsync every artifact, write and fsync the `COMMITTED` sentinel last
(`committed-at` plus `pid`), then flock the parent directory and rename: the live
profile moves aside to a backup, the txn dir becomes the profile, the parent is
fsynced, and the backup is removed. On any exception the txn dir is removed and
the original profile is untouched. Loaders refuse a profile whose `COMMITTED` is
missing or carries git-merge conflict markers.

Protocol artifacts — the manifest, archetype/canonical/convention/rule
artifacts, the prose artifacts, `renames.json`, `counterexamples.json`, and
the cross-file indexes except the Ruby `constant_index.json` (the
`_PROTOCOL_FILES` set) — deliberately do not carry forward when the current
build did not rewrite them: a failed rebuild, or a derive whose detected
language no longer produces an index, drops the old copy rather than serving
it stale. Absence fails open in every reader, while stale bytes drive false
phantom-import and cross-file findings and would be silently re-trusted. The
siblings carried forward are the non-protocol files — `enforcement.json`,
`config.json`, `constant_index.json` — and any user-dropped content.

**Crash recovery.** `cleanup_orphan_tmp_dirs` runs before every bootstrap and
refresh. It restores a committed backup when the live profile is gone and sweeps
orphan transaction directories whose writer PID is dead.

**Cross-platform locking.** `locks.py` is the single locking layer. POSIX uses
`fcntl.flock`; Windows (no `fcntl`) uses `msvcrt.locking` over a one-byte
region, with a held lock normalized to the same `BlockingIOError(EAGAIN)` so
callers behave identically. The directory-handle rename lock has no Windows
equivalent, so on Windows it falls back to a sidecar `.chameleon.winlock` file.
Liveness checks never call `os.kill` on Windows (it would terminate the
process); they query `OpenProcess` instead.

**Refresh serialization.** `refresh_repo` holds an advisory flock on
`.refresh.lock`; a second concurrent refresh fails fast. A stale lock (dead PID
or aged out) is broken with a warning.

---

## State stores

Two SQLite databases — the per-repo `drift.db` and the single cross-repo
`index.db` — opened WAL with a busy timeout and `trusted_schema=OFF`, with
per-process retry-and-jitter on `SQLITE_BUSY`. (A read-only open applies only
the busy-timeout and `trusted_schema` pragmas; the WAL pragmas need write
access and would abort a read-only open.) The Bash exec log is NDJSON, not
SQLite (see below).

### drift.db (per-repo, `<data>/<repo_id>/drift.db`)

A cache plus three durable tables. Schema version 1; a corrupt file self-heals
by drop-and-recreate.

```sql
CREATE TABLE schema_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);

-- Per-edit confidence history (drift score source). Reset on re-derive.
CREATE TABLE edit_observations (
  id INTEGER PRIMARY KEY, rel_path TEXT NOT NULL, archetype TEXT,
  confidence_observed REAL, matched_canonical INTEGER NOT NULL DEFAULT 0,
  observed_at INTEGER NOT NULL);

-- Inline chameleon-ignore overrides. Durable, NOT reset on refresh.
CREATE TABLE rule_overrides (
  id INTEGER PRIMARY KEY, rel_path TEXT, rule TEXT NOT NULL, archetype TEXT,
  session_id TEXT, blanket INTEGER NOT NULL DEFAULT 0, observed_at INTEGER NOT NULL);

-- Per-edit postmortem log (recovery loop). Durable, NOT reset on refresh.
CREATE TABLE decision_log (
  id INTEGER PRIMARY KEY, rel_path TEXT NOT NULL, archetype TEXT,
  match_quality TEXT, confidence_band TEXT, violations_raised INTEGER NOT NULL DEFAULT 0,
  blockable_rules TEXT, outcome TEXT NOT NULL, content_digest TEXT,
  session_id TEXT, observed_at INTEGER NOT NULL);

-- Surfaced-finding ledger (the finding->fix loop). Durable, NOT reset on refresh.
CREATE TABLE judge_findings (
  id INTEGER PRIMARY KEY, session_id TEXT, lens TEXT NOT NULL, severity TEXT,
  rel_path TEXT, line INTEGER, anchor_digest TEXT, fingerprint TEXT NOT NULL,
  ws_root TEXT, status TEXT NOT NULL DEFAULT 'open',
  observed_at INTEGER NOT NULL, resolved_at INTEGER);
```

`observed_drift_score` is `clamp(0, 1, 1 - mean(confidence_observed))` over a
trailing window (default 14 days). The SessionStart banner fires only when the
score and the observation count both clear their floors (default 0.4 over at
least 10 observations) and a cooldown marker is older than its TTL (default 7
days). `rule_overrides` and `decision_log` are durable because the questions
they answer (is a convention fighting the team; what did chameleon know when
a defect escaped) span many profile revisions; they are bounded by an
age-then-recency trim, not drop-and-recreate. `judge_findings` backed the
pre-3.4.0 finding->fix ledger the same way (`fingerprint` the per-(lens,
file, locus) dedup key, `anchor_digest` the reviewed file's content digest at
review time, `status` walking open / addressed / resurfaced / ignored,
`ws_root` scoping each row to the workspace that wrote it) but is retired:
the async-first cutover moved the finding->fix loop to `review_ledger.py`'s
`findings_ledger.json` (see [Enforcement](#enforcement)), and nothing reads
or writes this table anymore. Its DDL is left in place (a stale
`judge_findings` row from before the cutover is not migrated — a documented,
low-impact gap) rather than dropped, since `drift.db` is a cache a schema
bump can drop-and-recreate freely anyway.

(There is no `files` table; the old per-file cache table was removed when it lost
its last reader.)

### index.db (single, `<data>/index.db`)

The cross-repo registry. Three tables: `schema_meta` (pins the schema
version), `repos` (composite primary key `(repo_id, repo_root)` so a monorepo
sub-workspace does not clobber the root row), and `file_clusters` (per-file
cluster assignment, used by refresh to decide partial versus full re-cluster).
`list_profiles` enumerates `repos` ordered by
`last_seen_at DESC, repo_id ASC` with keyset (not OFFSET) cursor pagination.

### Exec log and the HMAC key

Not SQLite: the Bash exec log is HMAC-signed NDJSON under
`${TMPDIR:-/tmp}/.chameleon_exec_log/<repo_id>/`, one record per Bash
invocation, storing the command's SHA-256 (never the command body, which can
carry secrets), exit code, duration, a privacy-preserving test-command
classification, and the HMAC. The key is 32 random bytes at
`~/.claude/hooks/.exec_hmac.key` (mode 0600, parent 0700, owner-checked,
overridable with `CHAMELEON_HMAC_KEY_PATH`). It fails loud, never silently
unsigned, and signs the session-disable markers, the review ledger, and the
attestation log too.

---

## The advisor daemon

The daemon (`daemon.py`) is a per-user performance layer that holds the profile
in memory and answers hot-path lookups over a unix socket. It is **POSIX only**
(it uses `AF_UNIX`; on Windows the hooks always run in-process) and never
load-bearing: a missing daemon means an in-process lookup, never a failure.

The socket is `<tmpdir>/chameleon-<uid>/d-<hash>.sock`, per-user, the hash
folding the plugin data dir + version tag (the tag includes a content
fingerprint of the source, so a code-only upgrade rotates the socket, and two
`CHAMELEON_PLUGIN_DATA` universes never cross-talk). It lives under a short
user-private tmp dir rather than the data dir because `AF_UNIX` caps
`sun_path` at ~104 bytes and a deep data dir made every bind fail; pidfile
and logs stay in the data dir. SessionStart fires
`ensure_daemon_async` as a best-effort
background spawn (and stops a stale daemon on an upgrade), so a warm daemon is
usually ready by the first edit. The hot path also self-heals: if the daemon is
absent, `get_pattern_context` fires `ensure_daemon_async` and falls back to an
in-process lookup for the current call, so the next call finds it ready. The
dispatch allowlist exposes only read tools (`get_pattern_context`, `detect_repo`,
`get_archetype`, `lint_file`, `invalidate_cache`, `ping`); no state-mutating tool
is reachable over the socket. It self-exits after an idle timeout
(`CHAMELEON_DAEMON_IDLE_TIMEOUT`, default 600 seconds).

---

## Security model

chameleon runs locally and treats the repositories it analyzes as **untrusted
input**. A committed profile, idioms file, or source file can be hostile.

- **Path safety (`safe_open`).** The single mandatory file-read helper rejects
  null bytes, Windows alternate data streams, `..` after NFC normalization, and
  forbidden segments (`.git`, `.ssh`, `.aws`, `.env` and variants, and more). It
  lstats before resolving (refusing symlinks), prefix-matches the realpath
  against the repo root, and caps the read size. The fd variants close the
  lstat-then-open race with `O_NOFOLLOW`.
- **Context sanitization.** Everything repo-derived passes through
  `sanitize_for_chameleon_context` before it lands in a `<chameleon-context>`
  block: zero-width and invisible-format unicode stripped first, then bidi
  controls (the Trojan-Source set), ANSI escapes, and C0 controls; NFC
  normalize; fold fullwidth angle brackets to ASCII; replace dangerous tokens
  (`</chameleon-context>`, `<system>`, ChatML boundaries) with a sanitized
  marker; and neutralize a forged status header. Untrusted repo content is
  additionally wrapped by `spotlight_untrusted` in a per-block provenance frame
  with an unpredictable nonce, telling the model the block is data to imitate,
  never instructions.
- **Canonical scans.** The secret, injection, and poisoning scans (see
  [Canonical selection](#canonical-selection)) keep a poisoned example out of
  the profile.
- **JSON hardening.** The profile parser caps nesting depth, rejects duplicate
  keys, and requires a top-level JSON object. (NFC normalization lives
  elsewhere: `safe_open` normalizes paths and `sanitize_for_chameleon_context`
  normalizes context output.)
- **Trust gate.** Untrusted profiles inject no canonical content; `idioms.md`
  and `principles.md` are injection-scanned at trust-grant time.
- **HMAC integrity.** The exec log, session-disable markers, pause markers,
  review ledger, and attestation are HMAC-signed with the per-user key. This is
  tamper-evident against other local users, not forgery-proof against the key
  holder. The `.trust` record itself is deliberately NOT signed (a planted
  record would flip a repo to trusted, but the 0700 data-dir root already
  blocks other local users, and a same-user process can read the key and
  forge any signature anyway); signing it would orphan every pre-existing
  grant on upgrade, so that trade-off is documented here rather than closed.

**Repo-code execution and network are opt-in only.** The defaults never run repo
code or touch the network behind your back. The exceptions, each an explicit
flag:

| Flag | Default | What it enables |
|---|---|---|
| `CHAMELEON_ALLOW_ESLINT_EVAL=1` | off | Load JS ESLint configs via Node `require`/`import` during bootstrap (executes repo code). |
| `CHAMELEON_ALLOW_DEP_AUDIT=1` | off | The `dep_audit` action shelling `npm audit`/`bundler-audit` (the only network path). |
| `CHAMELEON_ALLOW_TSC=1` | off | The auto-pass router's `tsc --noEmit` grounding run (repo tsc from `node_modules/.bin`). |
| `CHAMELEON_ALLOW_TESTS=1` | off | The auto-pass router's repo-local test run (vitest/jest from `node_modules/.bin`). |

The default-on production-ref `git fetch` is the one network path that is on by
default; it self-suppresses under CI, never runs on a hook hot path, and fails
open (see [Production-ref derivation](#production-ref-derivation)).

**Intent capture privacy.** The UserPromptSubmit capture persists only extracted
checkable tokens (numerals, code-shaped identifiers, quoted strings), a prompt
digest, and — as of v3.6.0 — a bounded set of verbatim scope-constraint
sentences (the intent contract's `scope_lines`, "don't touch X" / "only change
Y"), never raw prompt prose. A hard-secret scanner runs over the whole
prompt and fails closed (a hit persists zero tokens **and zero scope lines**),
each surviving token is re-scanned and dropped if credential-shaped, the scope
lines are themselves bounded and secret-scanned by `extract_scope_lines`, and a
prompt-borne `chameleon-ignore` cannot defeat redaction. Files are 0600 in the
0700 data dir and swept after a retention window. Kill switches:
`CHAMELEON_INTENT_CAPTURE=0` (disables capture entirely) and
`CHAMELEON_INTENT_CONTRACT=0` (drops the scope-line channel only).

Report vulnerabilities privately through a GitHub security advisory; see
[SECURITY.md](../SECURITY.md).

---

## Performance characteristics

The hot path is the PreToolUse hook. Its budget is the 3-second shell `timeout`
cap; the in-process advisor call is itself time-bounded and fails open, so a
slow lookup degrades to a banner rather than stalling the edit. Steady-state
context drops to a short pointer (around 30 tokens) once an archetype has been
seen this session; the full canonical witness is paid only on the first edit in
an archetype or after a violation.

The advisor daemon removes per-edit subprocess startup when present: a warm hook
is a socket round trip instead of a fresh interpreter. The extractors are
long-lived subprocesses that load the parser once and stream file paths, so a
large repo is one startup plus N parses across the corpus, not N startups.

Bootstrap time scales with file count: a few seconds for repos under a few
thousand files, longer for larger trees, and a hard refusal past 200k files
without an explicit `paths_glob`. Refresh of a production-pinned repo is a
tip-SHA check first: an unchanged tip is an instant noop.

`/chameleon-doctor` reports hook interpreter health, daemon liveness, HMAC key
state, and per-repo profile state. `plugin/bin/chameleon-statusline.sh` runs under a
sub-100ms budget and respects `CHAMELEON_DISABLE`.

---

## Configuration and environment

`.chameleon/config.json` is operator-managed and trust-hashed. All fields are
optional; a missing file means defaults. Unknown top-level keys and unknown keys
under `enforcement` are tolerated for forward compatibility; unknown keys under
`auto_refresh` and `trust` are rejected.

| Key | Default | Meaning |
|---|---|---|
| `production_ref` | `null` | Branch to derive from; `null` is a durable auto-lock opt-out. Security-validated branch name. |
| `canonical_ref` | `null` | Redirect profile reads to a committed snapshot at a ref. |
| `auto_rename` | `true` | Skip the rename interview in init. |
| `repo_uuid` | `null` | Stable identity for remote-less repos. |
| `auto_refresh.enabled` | `true` | Run the SessionStart auto-refresh. |
| `auto_refresh.drift_threshold` | `0.2` | Drift score that triggers auto-refresh. |
| `auto_refresh.max_age_hours` | `168` | Age that triggers auto-refresh. |
| `auto_refresh.fetch_production_ref` | `true` | The default-on production fetch before refresh. |
| `trust.auto_preserve_when` | `"always"` | `always` / `pulled_from_remote` / `null` re-grant policy. |
| `enforcement.mode` | `"enforce"` | `off` / `shadow` / `enforce`. |
| `enforcement.stop_backstop` | `true` | Stop-hook enforcement backstop. |
| `enforcement.stop_block_cap` | `3` | Max Stop blocks per session. |
| `enforcement.idiom_review` | `true` | Gates the async review job's idiom lens (`stop/lenses/idiom.py`). `false` is the durable per-repo off-switch. |
| `enforcement.idiom_judge` | `true` | VESTIGIAL: pre-3.4.0 this hardened the now-deleted Stop-hook idiom-review directive text. Still surfaced in `/chameleon-status` output but read by no live lens. |
| `enforcement.correctness_judge` | `true` | Gates the async review job's correctness lens. |
| `enforcement.duplication_review` | `true` | Gates the async review job's duplication lens. |
| `enforcement.multi_lens_review` | `true` | VESTIGIAL: pre-3.4.0 this coordinated the correctness+duplication gates into one pass. Every review is now one job running every active lens by construction, so this key is unread. |
| `enforcement.judge_crossfile_facts` / `judge_imported_definitions` / `judge_transitive_impact` | `true` | Judge prompt grounding blocks. |
| `enforcement.signature_contract_diff` | `true` | Deterministic caller-contract diff (tool-time). |
| `enforcement.stale_test_advisory` / `changeset_completeness` / `crossfile_existence_advisory` / `test_integrity_review` / `intent_scope_advisory` | `true` | Deterministic turn-end advisories. |
| `enforcement.crossfile_existence_block` | `false` | Opt-in deny: BLOCK a Stop where the turn removed a TS/Python export an indexed importer still uses (live-re-verified, HEAD-scoped, FP-free). Stop-only; `enforce` blocks, `shadow` logs `would_block`. Overridable with `chameleon-ignore removed-export-breaks-importers`. |
| `enforcement.calibration.auto_demote` | `true` | Whether refresh-time override-feedback demotion (`apply_override_feedback_demotion`) runs at all. `false` leaves every calibrated-active block rule's verdict untouched regardless of override rate. |
| `enforcement.calibration.override_rate_threshold` / `min_events` / `min_distinct_sessions` | `0.5` / `5` / `2` | Per-repo overrides for the demotion bar (fraction of fires overridden, minimum combined override+would-block events, minimum distinct sessions backing the evidence). Defaults equal the global `RULE_FP_DEMOTE_THRESHOLD` / `OVERRIDE_AUDIT_MIN_EVENTS` / `OVERRIDE_DEMOTION_MIN_SESSIONS` `_thresholds.py` values, so an absent section is byte-identical to pre-config behavior. |
| `review.surface_bar` | `"medium"` | The finding surface bar applied at `record_findings` time. `high` / `medium` / `low`; `medium` is the pre-config built-in behavior. A finding below the bar is `shelved` rather than delivered (see [Turn-end advisories](#turn-end-advisories-on-by-default-never-block)). |

The full list of environment variables (kill switches, opt-in gates, model
selectors, tuning knobs, and test-only overrides) lives in
[.claude/rules/environment-variables.md](../.claude/rules/environment-variables.md);
[CLAUDE.md](../CLAUDE.md#environment-variables) keeps the session-critical
subset and points there. Numeric tuning thresholds live in
`plugin/mcp/chameleon_mcp/_thresholds.py`, each overridable with a
`CHAMELEON_<NAME>` environment variable.

---

## Versioning and schema migrations

Engine versions stay in lockstep across six manifests, kept in sync by
`scripts/bump-version.sh` (the plugin cache is version-keyed). The current
engine is 3.6.0 and the current profile schema is 8.

**Compatibility contract for committed `.chameleon/`:** chameleon will not break
a committed profile schema without a major version bump. An engine reads any
schema at or below its own: an older-schema profile still loads and surfaces a
`/chameleon-refresh` recommendation. A newer-than-supported schema is refused
honestly at every surface: `load_profile_dir` raises, `detect_repo` reports
`profile_too_new` (never `profile_corrupted`), and `get_status` returns
`profile_too_new` instead of rendering a panel it cannot interpret.

**Migration contract** (see
[plugin/mcp/chameleon_mcp/profile/migrations/README.md](../plugin/mcp/chameleon_mcp/profile/migrations/README.md)):
every migration is idempotent, atomic (uses the same `atomic_profile_commit`),
a no-op when already at target, and ships a fixture pair CI asserts byte-equal.
No migration scripts exist yet: the v7-to-v8 bump (the cluster-signature metric
change) intentionally ships none, because an older profile still loads and a
re-bootstrap re-clusters under the new metric. The first migration script will
be authored when a bump needs one.

`drift.db` is a cache: drop-and-recreate is permitted on a schema bump (the
durable `rule_overrides`, `decision_log`, and `judge_findings` tables migrate
additively).
`index.db` uses additive-only `ALTER TABLE`.

**MCP tool surface** is a public API. Adding a tool, an optional field, or
loosening a validation is non-breaking. Renaming or removing a tool, reordering
positional arguments, tightening a validation, or changing a field's meaning
requires a major bump and a `### Breaking` CHANGELOG entry. v3 exercised
exactly this clause: 32 operator tools were folded into the three dispatchers
(`chameleon_lifecycle`, `chameleon_review`, `chameleon_telemetry`) with the
tool name becoming the `action` and the original arguments becoming `params`,
unchanged in name and value (the full mapping is the dispatcher tables in the
MCP server section and the v3 CHANGELOG entry); the 16 comprehension and
conformance tools stayed top-level, and the underlying Python functions kept
their signatures, so direct imports were unaffected.

---

## What stays human

These review classes reach no mechanism in the engine. State this plainly to
anyone considering dropping mandatory review.

- **Business-logic correctness** whether the endpoint returns the right field,
  whether the calculation is right, whether the feature does what the ticket
  asked. The LLM judge catches some logic-delta regressions; it does not
  understand the domain.
- **Novel security flaws** authorization-logic correctness (not just guard
  presence), IDOR, complex injection, crypto design, business-logic security
  holes. The dependency and secret checks cover known-shape risks only.
- **Architectural judgment** whether the change is the right design, whether a
  new abstraction earns its keep, whether the layering is sound beyond direction
  heuristics.
- **Intent and rationale** why a pattern exists and when it is correct to break
  it. `idioms.md` stores one sentence per idiom; it does not reason.
- **Unsupported languages** Go, Rust, Java, SQL, YAML return an empty
  snapshot and zero violations.
- **Anything below sample-size thresholds** sparse repos and brand-new
  directories where conventions are still forming.
- **Performance and runtime behavior** N+1 queries, quadratic loops, memory,
  race conditions, swallowed runtime errors. No dataflow or runtime analysis
  exists.

---

## Glossary

| Term | Definition |
|---|---|
| **archetype** | A category of file with shared patterns. A named cluster. |
| **AST** | Abstract syntax tree, from the TypeScript Compiler API, Prism, or libcst. |
| **atomic transaction** | Write all artifacts to `.chameleon.tmp/<txn-id>/`, write `COMMITTED` last, flock-serialize the rename. |
| **bootstrap** | First-time profile generation via `/chameleon-init`. |
| **canonical** | An archetype's reference example, trichotomized into witness, normative shape, and normative idiom. |
| **cluster signature** | The six-dimension `sig: file -> ClusterKey` that groups files into archetypes. |
| **content_signal** | A first-200-byte lexical directive (`use client`, `use server`, shebang, TS pragma). |
| **derivation_source** | Provenance recorded in `profile.json`: which tree (working or production worktree) the profile derived from. |
| **drift** | Divergence between current code and the profile, tracked per-edit in `drift.db`. |
| **engine** | The chameleon plugin code: hooks, MCP server, skills, extractors. Distinct from a profile. |
| **fail-closed / fail-open** | On error, deny (safety) versus allow with a warning (advisory). |
| **idiom** | A team-specific convention an AST cannot infer, stored as one JSON record per idiom in the `.chameleon/idioms/` store (`idioms.md` is a generated view). |
| **profile** | The per-repo committed data in `.chameleon/`. |
| **production_ref** | The locked branch a profile derives from. |
| **recency weight** | Canonical-selection weight that decays off a file's last git commit time (full 2x for a just-committed file, halving every `CANONICAL_RECENCY_HALF_LIFE_DAYS`, default 45), to defeat archive-majority repos; falls back to a 2x/90-day mtime step when git is unavailable. |
| **refresh** | Re-analyze and update the profile (`/chameleon-refresh`). |
| **sha_hint** | A non-crypto xxhash64 of file content, for fast change detection. |
| **trust** | Per-user approval of a committed profile (`/chameleon-trust`). |
| **witness** | The actual file chosen as an archetype's canonical. |
