# Dogfood SUMMARY — v0.5.2 cycle 2

Second dogfood pass after shipping v0.5.2. 9 apps tested end-to-end through 10 phases each, exercising every MCP tool surface and verifying every v0.5.2 fix against real-world data.

Reports under `REPORT-<app>.md`. Runner script: `run_dogfood.py`.

## Apps tested

| App | Shape | Files | Archetypes | Bootstrap | Result |
|---|---|---|---|---|---|
| bulletproof-react | monorepo-ts (root empty pkgjson) | 798 | n/a | FAILED | bootstrap rejected |
| Rails-B | rails-only | 868 | 5 | 2.4s | 42 PASS, 0 FAIL, 1 FINDING |
| TS-B | ts-only | 1,053 | 7 | 1.9s | 42 PASS, 0 FAIL, 1 FINDING |
| excalidraw | monorepo-ts | 629 | 4 | 6.8s | 41 PASS, 0 FAIL, 2 FINDING |
| forem | rails-with-frontend | 7,826 | 127 | 3.8s | 43 PASS, 0 FAIL, 1 FINDING |
| gitlabhq | rails-with-frontend | (~125k) | 13 | 15.8s | 42 PASS, 0 FAIL, 1 FINDING |
| mastodon | rails-with-frontend | 4,041 | 146 | 3.8s | 42 PASS, 0 FAIL, 2 FINDING |
| maybe | rails-with-frontend | 1,114 | 59 | 1.4s | 43 PASS, 0 FAIL, 1 FINDING |
| plane | monorepo-ts | 3,581 | 70 | 14.9s | 40 PASS, 0 FAIL, 3 FINDING |

## v0.5.2 fixes verified in real-world data

Across all 8 apps that successfully bootstrapped, every v0.5.2 fix passed its real-world reproducer:

| Fix | Confirmations |
|---|---|
| Bug 1 — API repo arg unify (`_resolve_repo_arg`) | 8/8 |
| Bug 2 — Idiom slug collision (4-hex retry) | 8/8 |
| Bug 3 — `list_profiles` JOIN with index.db | 8/8 |
| Bug 4 — `get_drift_status(path)` path-vs-id misroute | 8/8 |
| Bug 5 — `get_canonical_excerpt` typed error envelope | 8/8 |
| Bug 6 — `detect_repo` `$HOME` info-disclosure guard | 8/8 |
| Bug 7 — `suspicious_input` heuristic | 8/8 |
| `atomic_profile_commit` sibling preservation | 8/8 |
| Idiom language frontmatter + filter | 8/8 |
| `content_signal_match` wire-through `get_archetype` | 8/8 |
| Rails-aware naming priors | Forem, maybe, mastodon, Rails-B all clean |
| Extension-aware bucket | 8/8 |
| Monorepo bucket workspace name | 8/8 |
| `db/schema.rb` discovery exclusion | maybe, forem, mastodon (where present) |

## New bugs surfaced in this cycle

### Bug A — `get_canonical_excerpt` returns empty content when archetype has no witness (3 confirmations)

**Severity:** Medium.
**Confirmed on:** mastodon (archetype `class`), excalidraw (`cluster-f5192077`), plane (`cluster-000c659d`).
**Symptom:** `get_canonical_excerpt(<valid_repo_id>, <valid_archetype_name>)` returns `{"content": "", "witness_path": null, "truncated": false, "sha_hint": null}` with no error.
**Why it matters:** The v0.5.2 Bug 5 fix added a typed error envelope for INVALID repo_id, but the equally-silent "valid args, no witness available" path was not covered. Consumers (using-chameleon skill, IDE integrations) can't distinguish "no canonical exists for this archetype" from a transient I/O failure.
**Root cause hypothesis:** `canonicals.json` doesn't carry a witness for every archetype in `archetypes.json` (some archetypes are too sparse, or every candidate was secret-scanned out). The tool reads canonicals.json, finds the archetype absent, and returns empty rather than emitting a structured `no_witness` envelope.
**Suggested fix:** When `repo_id` and `archetype_name` both resolve but `canonicals[archetype_name]` is missing, return `{"status": "no_witness", "reason": "archetype is below the confidence threshold or all candidates contained secrets", "archetype_name": ...}` instead of empty content.

### Bug B — Monorepo with empty-root `package.json` fails bootstrap (1 confirmation, foundational pattern)

**Severity:** High.
**Confirmed on:** bulletproof-react (Turborepo-style: root `package.json` has only `scripts`; each `apps/<workspace>/` has its own `package.json` + `tsconfig.json`).
**Symptom:** `bootstrap_repo` returns `failed_unsupported_language` with "No TypeScript signals (tsconfig.json / package.json TS deps) and no Ruby signals detected".
**Why it matters:** This is a common modern monorepo layout (Turborepo, Nx, pnpm workspaces, Lerna). bulletproof-react alone serves as a community-standard example of "how to structure a React app." If chameleon can't bootstrap it, the on-ramp story is broken for any team using this layout.
**Suggested fix:** When the root has `package.json` but no TS deps AND no root-level `tsconfig.json`, scan one level down (`apps/*/`, `packages/*/`, `services/*/`) for tsconfig.json or TS-flavored package.json. If found, treat the repo as a TS monorepo and use the workspace dirs as bootstrap roots (`scan_roots`). Bound by a small fanout (e.g., 50 first-level dirs) so we don't accidentally walk a misconfigured tree forever.

### Bug C — Next.js / Remix archetypes don't get prior names (1 confirmation, severe at scale)

**Severity:** Medium.
**Confirmed on:** plane (70 archetypes, 35 are `cluster-<hash>` — 50% generic).
**Symptom:** Next.js / Remix conventional folders (`app/`, `pages/`, `routes/`, `api/`, `components/`, `lib/`, `hooks/`, `middleware/`) don't trigger a prior table the way Rails does, so half the archetypes ship with hash-based names.
**Why it matters:** Generic names make archetype review and `apply_archetype_renames` cumbersome. Rails got a 15-entry prior table in v0.5.2; the TypeScript story is still pattern-induced.
**Suggested fix:** Add a TypeScript-prior table parallel to the Rails one, gated by extension `.ts/.tsx/.jsx`. Cover:
- `app/(routes|api)/` (Next.js App Router) → `route-handler`, `page-component`
- `pages/api/` → `pages-api-handler`
- `pages/` (non-api, Next.js Pages Router) → `page-component`
- `components/`, `ui/` → `component`
- `hooks/` → `hook`
- `lib/`, `utils/`, `helpers/` → `util`
- `services/` → `service`
- `middleware/` → `middleware`
- `actions/` (Server Actions or Redux) → `action`
- `store/`, `stores/` → `store`
- `types/`, `models/` → `type-module`
- `queries/` → `query`

### Bug D — gitlabhq files_processed=6,574 of ~125k (1 observation, needs root-cause)

**Severity:** Medium (silent under-coverage).
**Observation:** gitlabhq is a ~125k-file Rails+JS monorepo. v0.5.2 bumped `REPO_SIZE_GUARD` to 100,000, but only 6,574 files reached clustering (5% of the disk file count, 6.5% of the cap).
**Hypotheses:** (1) Aggressive exclusions (vendor, public/uploads, app/assets/images, etc.) are correctly skipping non-source. (2) Sparse-cluster pruning is dropping legit-but-distinct source files. (3) Discovery walks but stops on a non-existing language signal.
**Suggested fix:** Add an instrumentation envelope field to `bootstrap_repo`: `discovered_files_pre_exclusion`, `discovered_files_post_exclusion`, `clustered_files`, `sparse_dropped_files`. Today's `files_processed` is the post-clustering count alone, which makes coverage analysis impossible without re-running discovery manually.

### Bug E — gitlabhq Rails+JS hybrid not detected (1 confirmation)

**Severity:** Low.
**Confirmed on:** gitlabhq — has `Gemfile` and `package.json` and `app/javascript/` but `language_hint` reported `None` for secondary.
**Hypothesis:** The v0.5.1 hybrid detector (`_is_rails_with_frontend`) requires the triple `Gemfile + config/application.rb + app/javascript/`. gitlabhq has all three at the root, so this should fire. Re-test required — could be a runner artifact.
**Suggested fix:** Re-run `_is_rails_with_frontend` directly on gitlabhq's root and verify.

## Issues NOT to fix in v0.5.3

- **"Bug 1 FINDING" across 8 reports**: false positive from the runner's annotation logic (the fallback PASS path was tagged FINDING). Runner-side cosmetic.
- **GitHub PAT direct-match missed during runner v1**: runner test data used 38-char body instead of the 36-char production length. Not a chameleon bug.
- **`content_signal_match_for` accepts str, parameter name says "bytes"**: docstring/naming nit. Production callers pass str correctly.
- **`disable_session` requires `session_id`**: documented in the function signature; runner just didn't pass it.

## v0.5.3 patch plan (proposed)

1. **Bug A** (medium): `get_canonical_excerpt` returns typed `no_witness` envelope instead of silent empty content. (~50 LOC, isolated to `tools.py`.)
2. **Bug B** (high): Workspace-level fallback in `_select_extractor` / `bootstrap` for empty-root monorepos. (~100 LOC across `bootstrap/orchestrator.py` + `bootstrap/discovery.py`. Needs a `scan_roots` envelope addition.)
3. **Bug C** (medium): TS-prior table in `bootstrap/naming.py` mirroring the Rails one. (~80 LOC.)
4. **Bug D** (medium): Instrumentation envelope fields. (~30 LOC.)
5. **Bug E** (low): Verify `_is_rails_with_frontend` triggers on gitlabhq; if not, broaden the heuristic. (~10 LOC + 1 test.)

Each follows the verify-before / verify-after / code-review discipline.

## Deferred to v0.6 (from v0.5.1 dogfood that remained after v0.5.2)

11 findings were already deferred in CHANGELOG v0.5.2's "Deferred to v0.6" section. This cycle adds:
- Bug D's instrumentation concern (which can be deferred if cycle 2 ships Bugs A+B+C as v0.5.3).

## Notes on testing methodology

- `run_dogfood.py` exercises chameleon as a Python library (importing `chameleon_mcp.tools` directly), not through the MCP stdio JSON-RPC layer. The MCP transport is unit-tested separately (`mcp_protocol_test.py`).
- Each app gets an isolated `CHAMELEON_PLUGIN_DATA` tmpdir so trust grants and index.db state don't bleed across apps.
- Two internal repos (anonymized in this report as `Rails-B` and `TS-B`) were tested with real paths; their per-app reports are intentionally excluded from the public commit. Row totals here reflect those runs.
