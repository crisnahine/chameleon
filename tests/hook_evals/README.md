# tests/hook_evals

Deterministic synthetic-file scenario suite for chameleon's pattern advisory.

See `docs/superpowers/specs/2026-05-12-hook-evals-design.md` for the full design.

## Run

Default (MCP layer, fast, deterministic):

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/hook_evals/runner.py
```

Full hook plumbing (opt-in, exercises `hooks/preflight-and-advise`):

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/hook_evals/runner.py --full
```

`--full` mode silently skips when bash, the hook script, or the venv python is missing.

## Adding a scenario

1. Pick a fixture (`tests/fixtures/eval_repos/ts_minimal` or `ruby_minimal`).
2. Create `scenarios/<lang>/<NN>-<name>.json` with at minimum:

   ```json
   {
     "name": "short human label",
     "fixture_repo": "ts_minimal",
     "file_path": "src/utils/new.ts",
     "file_content": "...",
     "trust_state": "trusted",
     "expected": {"archetype_name": "<archetype-from-canonicals.json>"}
   }
   ```

3. Run the runner. The scenario passes when assertions match.

Keep new scenarios archetype-shaped (one per archetype), not bug-driven.

## Updating fixture profiles

After a chameleon profile-schema change:

```bash
scripts/refresh_eval_fixtures.sh --check    # dry run
scripts/refresh_eval_fixtures.sh --apply    # write
```

`--apply` regenerates `.chameleon/` with `now=1700000000` for deterministic witness selection. Commit the resulting diff.

## Internals

- `runner.py` discovers scenarios via `glob('scenarios/**/*.json')`, sorted.
- Each scenario gets its own tmpdir for repo and plugin-data, isolated via `CHAMELEON_PLUGIN_DATA`.
- `--full` mode pipes a synthetic PreToolUse event through `hooks/preflight-and-advise` and parses the advisory from `hookSpecificOutput.additionalContext` or top-level `additionalContext`.
- Fail-open detection watches `~/.local/share/chameleon/.hook_errors.log` (hardcoded path in the bash hooks; not redirectable via env var). `--full` mode appends one line to that log on hook failure, an accepted cost.
