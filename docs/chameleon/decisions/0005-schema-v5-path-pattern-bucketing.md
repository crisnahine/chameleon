# ADR-0005: Schema v5 — top-level-aware path-pattern bucketing

> **Status:** Accepted
> **Date:** 2026-05-11
> **Deciders:** Cris Nahine

## Context

The cluster signature function's `path_pattern_bucket` component is
the first dimension of the 7-tuple `sig: file → ClusterKey`. It
determines which files cluster together by directory shape. Schema
v4 (shipped in v0.1.x) computed the bucket as `parts[-3:-1]` — the
two directory segments immediately enclosing the file.

The v0.2.0 external audit
([chameleon-test-report.md][audit] in the project repo) demonstrated
that this bucketing collapses meaningfully-different file roles in
common Rails layouts:

| File | v4 bucket |
|---|---|
| `app/controllers/api/v1/addresses_controller.rb` | `"api/v1"` |
| `spec/controllers/api/v1/addresses_controller_spec.rb` | `"api/v1"` |
| `app/admin/listings/foo.rb` | `"admin/listings"` |
| `spec/admin/listings/foo_spec.rb` | `"admin/listings"` |

Because `app/` files and `spec/` files landed in the same bucket,
they entered the same cluster. `get_archetype`'s cluster-size
tiebreak then routinely returned the spec-shaped archetype when
asked about an `app/` file, since Rails apps frequently have more
spec files than source files. The model was being given spec
canonicals as context for controller edits.

Two alternative bucket functions were considered:

- **`parts[:2]`** (top 2 segments) — `"app/controllers"` vs
  `"spec/controllers"`. Correctly disambiguates `app/` from
  `spec/`, but collapses `app/controllers/api/v1` and
  `app/controllers/admin` into the same bucket, which is too
  coarse for any Rails repo with more than a couple controller
  subnamespaces.
- **`parts[-3:-1]`** (v4's choice) — preserves enclosing-directory
  granularity but loses the top-level distinction.

Neither single approach worked for both shallow and deep layouts.

## Decision

Bump the profile schema version from 4 to 5. The new bucketing
function (`mcp/chameleon_mcp/signatures.py::path_pattern_bucket_for`,
~line 98) combines the top-level segment with the enclosing-directory
segments:

```
if len(parts) >= 4:
    return f"{parts[0]}/{parts[-3]}/{parts[-2]}"
return f"{parts[0]}/{parts[-2]}"
```

Examples (taken from the docstring):

| File | v5 bucket |
|---|---|
| `app/controllers/api/v1/users.rb` | `"app/api/v1"` |
| `spec/controllers/api/v1/users_spec.rb` | `"spec/api/v1"` |
| `app/models/listing.rb` | `"app/models"` |
| `src/components/base/Button.tsx` | `"src/components/base"` |
| `src/components/Button.tsx` | `"src/components"` |
| `Gemfile` (1 part) | `"(root)"` |

`app/` and `spec/` always disambiguate. Shallow paths reduce to
`parts[0]/parts[-2]`, matching v4's behavior on those paths.

The bootstrap orchestrator also now relativizes file paths to the
repo root *before* bucketing, so cluster patterns generated at
bootstrap time match what `get_archetype` computes at runtime
(the v0.1.1 code had subtly different inputs in the two paths).

`PROFILE_SCHEMA_VERSION` bumped to 5 in
`mcp/chameleon_mcp/bootstrap/orchestrator.py`; `CURRENT_SCHEMA_VERSION`
bumped to 5 in `mcp/chameleon_mcp/profile/schema.py`.
`SUPPORTED_SCHEMA_RANGE = (CURRENT_SCHEMA_VERSION - 1, CURRENT_SCHEMA_VERSION)`.

## Consequences

### Positive consequences

- `app/` and `spec/` files no longer collide. The audit's primary
  finding — `get_archetype` returning spec archetypes for `app/`
  files — is fixed at the bucketing layer rather than papered
  over downstream.
- Shallow-path behavior is preserved. `app/models/foo.rb` still
  buckets to `"app/models"`, so existing model archetypes carry
  forward in shape.
- The fix is one function, ~6 lines of code change. The 7-tuple
  signature schema itself didn't change shape, only the bucket
  derivation.
- `tests/v0_2_regression_test.py` section "Medium:
  path_pattern_bucket_for disambiguates app/ from spec/" pins the
  fix with three app/spec pairs plus shallow- and deep-path
  spot-checks.

### Negative consequences / trade-offs

- **Breaking change for committed profiles.** Re-bootstrap is
  required to migrate a v4 profile to v5. There is no in-place
  migration script because the bucket values themselves change
  — cluster identities differ between v4 and v5, so a v4
  `archetypes.json` cannot be rewritten into a valid v5 file
  without re-clustering from scratch.
- **Trust grants invalidate.** The rebuilt v5 profile has a
  different `profile.json` SHA, so existing trust records flip
  to `stale` and users must re-grant via `/chameleon-trust`.
  Documented in the v0.2.0 CHANGELOG under "Breaking."
- **`ENGINE_MIN_VERSION` bumped to `0.2.0`.** Older engines
  refuse to load v5 profiles. This is the contract.

### Risks accepted

- Some deep-but-symmetric layouts (e.g.,
  `packages/a/src/x.ts` vs `packages/b/src/x.ts` in a monorepo)
  still bucket together under v5 — they share `parts[0]="packages"`,
  `parts[-3]`, `parts[-2]`. This may or may not be desired;
  for monorepos with distinct package-scoped conventions, the
  current bucketing is too coarse. We accept that limitation
  for v0.2 and defer monorepo-aware bucketing to a future schema
  revision if dogfooding surfaces a real case.
- The audit's downstream fix proposal was different — it
  blamed the substring fallback in `tools.py:127`. We verified
  via a second independent agent that the upstream bucketing
  was the real cause. Fixing the bucket is the principled fix;
  the substring fallback in tools.py remains as a safety net.

## Alternatives considered

### A. Patch the downstream `tools.py:127` substring fallback

What the original audit report proposed. Rejected because the
bucket function is the canonical input to `get_archetype` and
to `merge_profiles`; fixing only the downstream lookup leaves
the bootstrap clustering generating wrong clusters that the
runtime then has to second-guess. The principled fix is at the
source.

### B. `parts[:2]` (top 2 segments)

Too coarse. Collapses `app/controllers/api/v1` and
`app/controllers/admin` and `app/services` into one bucket.
Rails repos with even modest controller namespacing would cluster
incoherently.

### C. Stay on v4, document the bug

Rejected because the bug demonstrably degrades the model's
context on every controller edit in a Rails repo. The
backwards-compatibility cost of a schema bump is one re-bootstrap
+ one re-trust per user; the cost of leaving the bug in is
ongoing.

## References

- `mcp/chameleon_mcp/signatures.py::path_pattern_bucket_for` — the bucketing function and its docstring
- `mcp/chameleon_mcp/bootstrap/orchestrator.py` — `PROFILE_SCHEMA_VERSION = 5`
- `mcp/chameleon_mcp/profile/schema.py` — `CURRENT_SCHEMA_VERSION = 5`, `SUPPORTED_SCHEMA_RANGE`
- `tests/v0_2_regression_test.py` — bucketing regression pins
- `CHANGELOG.md` — v0.2.0 entry, "Medium (schema-breaking)" item
- `ARCHITECTURE.md#cluster-signature-function` — original signature contract

[audit]: https://github.com/crisnahine/chameleon/blob/main/docs/chameleon-test-report.md
