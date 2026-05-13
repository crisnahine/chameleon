# Clustering fixes: Option 1 + Option 4

## Problem

Two orthogonal bugs cause chameleon to produce far too many archetypes on real
codebases, making most `get_pattern_context` calls return `archetype=null`.

**Service-v1-rb mislabel (Option 1 target).** Files in `app/services/zoom/` and
`app/services/billing/` differ by one `CallNode` in their top-level AST shape
- one has `('ClassNode',)`, another has `('CallNode', 'ClassNode')`. The
7-tuple key is exact, so they land in different tight clusters and get named
`service`, `service-v1`, `service-v2`, etc., none with enough members to be a
canonical. The loose-merge pass (`clustering.py:324-393`) only runs on sparse
clusters, so if these clusters each cross the sparse threshold they never merge.

**app/services/ fragmentation (Option 4 target).** `path_pattern_bucket_for`
uses the formula `parts[0]/parts[-3]/parts[-2]` for paths with 4+ segments
(`signatures.py:178`). On ef-api this turns `app/services/zoom/recordings.rb`
into bucket `app/services/zoom` and `app/services/billing/invoices.rb` into
`app/services/billing`. These are the same logical layer (`app/services`) but
different buckets, splitting what should be one `service` archetype across
dozens. Empirically: ef-api ends up with 213 archetypes total, 6 path buckets
split unnecessarily; ef-client 139 archetypes, 9 split.

---

## Option 1: Fuzzy top_level_node_kinds

### Code site

- `mcp/chameleon_mcp/signatures.py:284` - `compute_signature` currently writes
  `top_level_node_kinds=tuple(top_level_node_kinds)` into `ClusterKey`.
- `mcp/chameleon_mcp/bootstrap/clustering.py:302` - after `_loose_merge_sparse_clusters`
  returns, add a new call to `_tight_pass_shape_merge` on the result.
- Add `CLUSTER_SHAPE_JACCARD_THRESHOLD` to a new `mcp/chameleon_mcp/_thresholds.py`.

### Before / after example

Files: `app/services/zoom/recordings.rb`, `app/services/billing/invoices.rb`.

Before (current depth-3 bucket, separate keys):
```
ClusterKey(path_pattern_bucket="app/services/zoom:rb", top_level_node_kinds=("ClassNode",), ...)
ClusterKey(path_pattern_bucket="app/services/billing:rb", top_level_node_kinds=("CallNode","ClassNode"), ...)
```
(These don't merge even with Option 4 alone because their `top_level_node_kinds`
differ; Option 1 is what fixes the merge.)

After Option 1 alone (still depth-3 bucket, but tight-pass merge applies):
- Both clusters share `(path_pattern_bucket, default_export_kind, jsx_present)`.
- Jaccard(`{"ClassNode"}`, `{"CallNode","ClassNode"}`) = 1/2 = 0.5 - below the
  0.7 threshold, so they don't merge on Option 1 alone.
- However, adding Option 4 first (depth-2 bucket `app/services:rb`) makes the
  `path_pattern_bucket` identical, and the 0.5 Jaccard would then trigger the
  loose-merge pass. The two options compose.

Better example that Option 1 fixes independently: 4-way component-base split
in ef-client where all four clusters share `path_pattern_bucket="src/components:tsx"`
and differ only on whether `TypeAliasDeclaration` is in the kinds set. Jaccard
of e.g. `{"FunctionDeclaration","TypeAliasDeclaration"}` vs
`{"FunctionDeclaration"}` = 1/2 = 0.5. At threshold 0.7 these still don't
merge. But `{"FunctionDeclaration","ExportDeclaration","TypeAliasDeclaration"}`
vs `{"FunctionDeclaration","ExportDeclaration"}` = 2/3 = 0.67 - still just
below. At 0.6 they would; this is the rationale for making the threshold
env-tunable (see Knob section below).

### Cluster key change

The key itself does NOT change shape. `ClusterKey.top_level_node_kinds` stays a
`tuple[str, ...]` (exact match for the tight pass). The merge happens in a
post-clustering step that compares the already-formed clusters, not inside the
key. This preserves idempotence and cache stability.

### Threshold rationale

Default 0.7. Two clusters with Jaccard >= 0.7 share at least 70% of their
node-kind vocabulary - they almost certainly represent the same architectural
role. Below 0.5 the merge is lossy (that's the existing loose-merge threshold,
which already marks results as `tier="loose"`). 0.7 is conservative enough
to avoid over-merging genuinely different archetypes (a `ClassNode`-only cluster
vs a `FunctionDeclaration`-only cluster scores 0.0 and is correctly left split).

### Interaction with existing loose-merge pass

The existing `_loose_merge_sparse_clusters` (`clustering.py:324-393`) runs on
sparse clusters only, at Jaccard >= 0.5. The new tight-pass merge runs AFTER
`_loose_merge_sparse_clusters` and operates on any cluster size, using a
stricter threshold (0.7). Order:

1. Tight clustering by exact key (by_key loop, `clustering.py:243-274`).
2. `_loose_merge_sparse_clusters` - sparse clusters only, Jaccard >= 0.5.
3. NEW: `_tight_pass_shape_merge` - all remaining clusters, Jaccard >= 0.7,
   grouped by `(path_pattern_bucket, default_export_kind, jsx_present)`.
4. Sort by size descending.

Running step 3 after step 2 means a cluster that was already loose-merged can
still participate in the shape merge. That's fine because step 3 uses the
merged cluster's aggregate shape (union of all member kinds). The result is
marked `cluster_tier="shape-merged"` to distinguish it from `"loose"`.

---

## Option 4: Path bucket depth = 2

### Code site

- `mcp/chameleon_mcp/signatures.py:177-180` - the `elif len(parts) >= 4:`
  branch currently reads `bucket = f"{parts[0]}/{parts[-3]}/{parts[-2]}"`.
  Change to `bucket = f"{parts[0]}/{parts[1]}"` for non-monorepo paths with
  4+ segments. Store `parts[-3]/parts[-2]` as a `sub_bucket` in cluster
  metadata (not in `ClusterKey`).
- Add `CLUSTER_PATH_BUCKET_DEPTH` to `mcp/chameleon_mcp/_thresholds.py`.

### Before / after example

**app/services/ (ef-api):**
| File | Before (depth-3) | After (depth-2) |
|---|---|---|
| `app/services/zoom/recordings.rb` | `app/services/zoom:rb` | `app/services:rb` |
| `app/services/billing/invoices.rb` | `app/services/billing:rb` | `app/services:rb` |
| `app/services/zoom/base.rb` | `app/services/zoom:rb` | `app/services:rb` |

All three now share the same `path_pattern_bucket`, so the tight clustering
pass groups them together before any merge step runs.

**src/pages/ (ef-client):**
| File | Before | After |
|---|---|---|
| `src/pages/dashboard/index.tsx` | `src/pages/dashboard:tsx` | `src/pages:tsx` |
| `src/pages/listings/show.tsx` | `src/pages/listings:tsx` | `src/pages:tsx` |

Expected reduction: 1134 files in `src/pages/` that currently produce ~62
archetypes should collapse toward a handful.

**Monorepo case (apps/web and apps/admin):**

`apps/web/components/Button.tsx` and `apps/admin/components/Button.tsx`:
- The `parts[0] in _MONOREPO_WORKSPACE_ROOTS` branch (`signatures.py:165-176`)
  still fires first (depth-3 formula `parts[0]/parts[1]/parts[2]`), giving
  `apps/web/components` and `apps/admin/components` respectively. These
  correctly stay split. The depth-2 change only affects the non-monorepo
  `elif` branch at line 177. Monorepo paths are unaffected.

**Second monorepo case (packages/propel/src/services/):**

`packages/propel/src/services/billing.ts` hits the monorepo branch and
produces `packages/propel/src`. `packages/element/src/services/billing.ts`
produces `packages/element/src`. Different buckets - correct.

**Third case: flat repo with many subsystem dirs:**

`src/api/v1/users.ts` and `src/api/v2/users.ts`:
- Before: `src/api/v1` vs `src/api/v2` (split).
- After: `src/api` vs `src/api` (merged). This is the correct behavior - v1
  and v2 of the same API layer share the archetype. The `sub_bucket` metadata
  records `api/v1` and `api/v2` if a consumer ever needs to distinguish them.

### Sub-bucket metadata shape

Each `Cluster` gains an optional field `sub_buckets: list[str]` populated
from the original per-file `parts[-3]/parts[-2]` values (de-duped). This is
not part of `ClusterKey` (not used for equality) but is written into
`archetypes.json` under `"sub_paths"` so users can see which subdirectories
contributed to the archetype. `naming.py` reads `sub_buckets` only for
disambiguation suffixes - it doesn't affect the primary name.

### Knob

`CLUSTER_PATH_BUCKET_DEPTH` in `_thresholds.py`, default `2`. At depth 1
you get `parts[0]` only (too coarse). At depth 3 you get current behavior.
The env var overrides the `elif len(parts) >= 4` branch only; the monorepo
branch is unaffected.

---

## Migration

### Schema bump decision

NO schema bump to `PROFILE_SCHEMA_VERSION` (currently `7`,
`orchestrator.py:320`). The JSON structure of `archetypes.json`,
`canonicals.json`, and `profile.json` is unchanged. Consumers load the same
keys in the same places.

Add `"clustering_algorithm_version": 2` to `profile_data` in
`orchestrator.py:1419-1440`. Readers that don't know this field ignore it;
v0.5.9+ can detect pre-fix profiles by checking `< 2` or absent.

### Re-bootstrap policy

Existing profiles load and work unchanged. The clustering improvements take
effect only on the next full bootstrap triggered by `/chameleon-refresh` or
`/chameleon-init`.

**One-time hint:** in `hook_helper.py`, when loading a profile, if
`profile_data.get("clustering_algorithm_version", 1) < 2`, emit a single
advisory (guarded by a session-state flag so it fires at most once per
session):

> "Your chameleon profile predates v0.5.9 clustering improvements. Run
> /chameleon-refresh for better archetype grouping."

The hint is suppressed if the user has already run `/chameleon-disable` or
`/chameleon-pause-15m`.

---

## Test plan

### New test files

- `tests/clustering_jaccard_test.py` - unit tests for `_tight_pass_shape_merge`:
  - Two clusters sharing bucket + default_export_kind + jsx_present with
    Jaccard >= 0.7 merge into one `shape-merged` cluster.
  - Two clusters with Jaccard < 0.7 remain split.
  - A cluster already above sparse threshold (tight) still participates.
  - Threshold override via `CLUSTER_SHAPE_JACCARD_THRESHOLD=0.5` env var.
  - Ordering: loose-merged clusters can participate in shape merge.

- `tests/clustering_path_bucket_depth_test.py` - unit tests for depth-2 bucket:
  - `app/services/zoom/recordings.rb` and `app/services/billing/invoices.rb`
    produce the same bucket.
  - `apps/web/components/Button.tsx` and `apps/admin/components/Button.tsx`
    stay split (monorepo branch unaffected).
  - `CLUSTER_PATH_BUCKET_DEPTH=3` restores the old depth-3 behavior.
  - `sub_bucket` metadata contains the original `parts[-3]/parts[-2]` values.
  - Shallow paths (3 segments) are unaffected (existing `else` branch).

### Existing tests that need updating

These test files pin OLD bucket values or cluster-split counts that the depth-2
change will break:

- `tests/v0_5_2_clustering_test.py:303` - asserts
  `path_pattern_bucket_for("app/controllers/api/v1/foo.rb") == "app/api/v1"`.
  After depth-2, this becomes `"app/controllers"`. Update assertion.
- `tests/v0_5_2_clustering_test.py:239-240` - asserts the deep path bucket
  starts with `"app/"` (still true, no change needed) but the exact value
  `"app/api/v1"` appears in the error message; update the expected string.
- `tests/v0_2_regression_test.py:239-240` - same `app/api/v1` expectation.
  Change to `app/controllers`.

Grep for any test that compares `path_pattern_bucket_for(...)` to a 3-segment
string of the form `"X/Y/Z"` on a non-monorepo path with 4+ segments - those
are the only ones affected.

No existing test pins `('ClassNode',)` vs `('CallNode', 'ClassNode')` as
DIFFERENT clusters - the shape-merge step introduces new behavior rather than
changing any assertion that currently passes.

---

## Expected outcomes on ef-api and ef-client

| Metric | ef-api before | ef-api after | ef-client before | ef-client after |
|---|---|---|---|---|
| Total archetypes | ~213 | ~80-100 | ~139 | ~60-80 |
| `app/services/` archetypes | ~30+ | ~3-5 | n/a | n/a |
| `src/pages/` archetypes | n/a | n/a | ~62 | ~8-12 |
| `archetype=null` rate | >50% | <15% | >40% | <15% |

These are estimates based on the depth-3 fragmentation pattern. Actual counts
depend on how many tight clusters survive after the Jaccard step at 0.7.

---

## Out of scope

- Ruby extractor changes (`prism_dump.rb`) - both fixes operate on the
  clustering layer and are language-agnostic.
- Changing `import_module_set_hash` - still exact-match; import set diversity
  is a feature, not a bug.
- `_loose_merge_sparse_clusters` threshold adjustment - 0.5 stays; the new
  step adds a higher-threshold pass, it does not replace the existing one.
- Canonicals selection (`canonicals.py`) - unchanged; once clusters are
  correct, canonical selection already picks the most representative member.
- Profile migrations for existing on-disk profiles - no structural change, so
  no migration needed.
