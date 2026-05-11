# chameleon calibration harness

Skeleton for Phase 6 (conformance + cost-ceiling measurement). v0.4.0
ships the scaffolding; the **measurement numbers** (target: 80%
archetype-match conformance across 3 TS repos) require external test
corpora that this project does not yet own.

## What the harness does

For each repo in the corpus, the harness:

1. Runs `/chameleon-init` (or `bootstrap_repo`) cold.
2. Records `archetype_count`, `files_processed`, `duration_ms`,
   `bootstrap_cost_dollars` (always `$0.00` today — bootstrap is local).
3. Picks a stratified sample of test files (one per archetype, by
   `cluster_size DESC`).
4. For each sample, calls `get_pattern_context(file_path)` and checks:
   - Was an archetype matched? (`archetype is not None`)
   - Did the match's `paths_pattern` actually contain the file's path?
   - Is `confidence_band` in `("high", "medium")`?
5. Aggregates into a conformance score per repo and rolled up across
   the corpus.

## Running the harness

The harness reads corpus paths from `tests/calibration/corpus.json`
(gitignored — corpus paths are per-developer). Example schema:

```json
{
  "repos": [
    {"name": "ts-app-a", "path": "/abs/path/to/repo-a", "language": "typescript"},
    {"name": "rails-app-b", "path": "/abs/path/to/repo-b", "language": "ruby"}
  ]
}
```

Then:

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/calibration/harness.py
```

If `corpus.json` is missing, the harness exits 0 with `"status":
"no_corpus_configured"` and an `N/A` row per metric. CI uses this fall
through so the calibration job stays green on PRs.

## Calibration parameters under test

Per `docs/chameleon/MAINTAINER.md` and `ARCHITECTURE.md`:

| Param | Default | Source |
|---|---|---|
| `recency_weight` | `2.0` | `bootstrap/canonical.py:RECENCY_WEIGHT_MULTIPLIER` |
| `recency_window_days` | `90` | `bootstrap/canonical.py:RECENCY_WINDOW_DAYS` |
| `min_cluster_size` | `5` | `bootstrap/clustering.py:SPARSE_CLUSTER_THRESHOLD` |
| `bimodal_threshold` | `0.6` | `bootstrap/clustering.py:BIMODAL_DOMINANT_SHARE_THRESHOLD` |
| `repo_size_guard` | `50_000` files | `bootstrap/orchestrator.py` |
| `ast_node_ceiling` | `50_000` nodes | TS extractor cap |
| `MCP_timeout` | `2.0` seconds | `hooks/preflight-and-advise` |
| `path_pattern_bucket_depth` | `3` (v5) | `signatures.py:path_pattern_bucket_for` |
| `top_level_node_kinds_compare` | multiset-with-extras | `lint_engine.py` |

When real corpus runs land, every parameter above gets a calibration row
in the harness output. Discrepancy >10% from the target prompts an ADR
proposal.

## Status (v0.4.0)

Harness scaffolding complete. Numbers are not yet measured.
Open issue: identify and check in a corpus of 3 TS + 1 Rails repos for
the maintainer to dogfood against. Until then, all calibration outputs
print `N/A`.
