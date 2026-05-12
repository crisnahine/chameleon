# Design: hook eval scenario harness

**Status:** Approved design, 2026-05-12.

## Motivation

Today we have two test layers:

- Unit + integration tests (`tests/*_test.py`): correctness of individual functions, modules, hooks.
- Calibration harness (`tests/calibration/harness.py`): aggregate conformance across a private corpus of real repos; slow, gated on `corpus.json`.

Nothing in between answers the deterministic, contributor-friendly question: *"given this synthetic file inside a known profile, does `get_pattern_context` (and the hook on top of it) produce the right advisory?"*

When the canonical builder, archetype matcher, or path-bucketing logic changes, the only signal today is calibration drift on real repos. That's high signal but slow, expensive, and per-developer. New contributors can't run it.

Inspiration: double-shot-latte solves an analogous problem (probabilistic LLM judgments) with a JSON scenario suite + per-scenario `expected_decision` + multi-run aggregation. We borrow the shape and drop the multi-run aggregation (our advisory is deterministic).

## Non-goals

- Replacing the calibration harness. It runs against real repos and measures different things.
- Replacing `pretooluse_hook_test.py`. That's a live Claude smoke test; this is synthetic.
- Exercising bootstrap. The `.chameleon/` profile is checked into fixture repos. Bootstrap is covered by other tests.

## Design

### File layout

```
tests/
  hook_evals/
    README.md
    runner.py
    scenarios/
      _meta/
        expected_fail.json
      ts/
        01-utility-export.json
        02-react-component.json
        ...
      ruby/
        01-active-record-model.json
        02-controller-action.json
        ...
  fixtures/
    eval_repos/
      ts_minimal/
        .chameleon/        (checked in: profile.json, canonicals.json, rules.json)
        src/utils/example_util.ts
        src/components/ExampleComponent.tsx
        src/utils/example_util.test.ts
        package.json
      ruby_minimal/
        .chameleon/        (checked in)
        app/models/example.rb
        app/controllers/examples_controller.rb
        spec/models/example_spec.rb
        Gemfile
scripts/
  refresh_eval_fixtures.sh
```

### Fixture repos

Two minimal repos, ~5 to 10 source files each, covering 3 to 4 archetypes apiece. The entire `.chameleon/` directory is committed.

**Why checked-in profiles:** scenarios test `(file_content, profile) -> advisory`. Re-bootstrapping on every run would have scenario expectations chase profile churn instead of catching advisory regressions. The bootstrap algorithm has its own tests and a separate calibration harness.

**Refresh:** `scripts/refresh_eval_fixtures.sh` re-bootstraps both fixture repos in place and re-runs the scenario suite so the maintainer can review the expected-vs-new diff. Run intentionally on profile schema bumps, not in CI.

### Scenario schema

```json
{
  "name": "ts utility file resolves to utility archetype",
  "description": "Files under src/utils/ should match the utility archetype",
  "fixture_repo": "ts_minimal",
  "file_path": "src/utils/new_helper.ts",
  "file_content": "export const helper = () => true;\n",
  "trust_state": "trusted",
  "expected": {
    "archetype_name": "utility",
    "canonical_excerpt_includes": ["export const"],
    "rules_must_include_substring": [],
    "rules_must_not_include_substring": [],
    "idioms_must_include_substring": []
  }
}
```

Fields:

- `fixture_repo`: which fixture under `tests/fixtures/eval_repos/` to use.
- `file_path`: path relative to the fixture root. Runner writes `file_content` here before the MCP call and removes it after.
- `trust_state`: `"trusted"` or `"untrusted"`. Runner sets state via `trust_profile` (or skips) before the call.
- `expected.archetype_name: null`: negative scenario, no archetype should match.
- `_includes` lists are AND'd. Empty list means no assertion for that field. Match is substring on the relevant text.

### Runner modes

```bash
# MCP layer (default; every CI run)
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/hook_evals/runner.py

# Full hook plumbing (opt-in)
CHAMELEON_RUN_FULL_EVALS=1 \
  cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/hook_evals/runner.py --full
```

**Default (MCP-layer) mode** per scenario:

1. Copy fixture repo to a `tempfile.TemporaryDirectory`.
2. Apply `trust_state` (call `trust_profile` if `trusted`).
3. Write `file_content` to `<tmp>/<file_path>`.
4. Call `chameleon_mcp.tools.get_pattern_context(<tmp>/<file_path>)` in-process.
5. Assert against `expected`.
6. Tmpdir auto-removed.

**`--full` mode** per scenario: same setup, then build a PreToolUse event JSON (`{tool_name, tool_input, session_id}`), pipe through `hooks/preflight-and-advise`, parse `additionalContext` from stdout, assert. Catches hook script regressions (`run-hook.cmd`, venv resolution, timeout wrapper, error logging) that MCP-layer mode skips.

Exit 0 if all pass, 1 if any fail. Output shape mirrors `tests/calibration/harness.py` (one JSON line summary + per-scenario stderr) for consistency.

### Self-test

`tests/hook_evals/scenarios/_meta/expected_fail.json` is a scenario whose `expected` block intentionally won't match reality (e.g. `archetype_name: "definitely_not_real"`). The runner verifies this scenario FAILS at the assertion step. If a bug ever causes the runner to always-pass, the meta-scenario catches it. The runner inverts the assertion specifically for files under `_meta/` and reports them inverted in the summary.

### CI integration

Add MCP-mode run to `tests/run_all_orders.py` as a 6th entry. Total runtime budget: under 5 seconds across the full scenario set.

`--full` mode is gated on `CHAMELEON_RUN_FULL_EVALS=1` (same opt-in pattern as `pretooluse_hook_test.py`'s reliance on `CHAMELEON_TEST_TS_REPO` / `CHAMELEON_TEST_RUBY_REPO`).

## Seed scenarios

TypeScript:

1. utility export at `src/utils/` -> `utility` archetype.
2. React component at `src/components/` (PascalCase, JSX) -> `component` archetype.
3. test file `*.test.ts` -> `test` archetype.
4. type-only file at `src/types/` -> `type-only` archetype if the profile distinguishes it, else `null`.
5. negative: `src/weird/_one_off.ts` -> `archetype: null`.

Ruby on Rails:

1. ActiveRecord model `app/models/foo.rb` -> `model` archetype.
2. Controller action `app/controllers/foos_controller.rb` -> `controller` archetype.
3. RSpec `spec/models/foo_spec.rb` -> `spec` archetype.
4. Service object `app/services/foo_service.rb` -> `service` archetype if present, else `null`.
5. negative: `app/weird/_one_off.rb` -> `archetype: null`.

Cross-cutting (one of each, either fixture):

- `trust_state: "untrusted"`: advisory still returned, but trust flag surfaced to caller.
- Repo missing `.chameleon/` directory: returns no-profile shape gracefully without exception.

## Risks

- **Fixture profile rot:** chameleon's profile schema evolves. When a schema field is renamed or dropped, fixture profiles become invalid. Mitigation: the refresh script regenerates in place; CI failure is informative when `canonical_excerpt_includes` no longer matches.
- **Overfitting:** every regression PR could add a scenario to silence its own failure. The corpus then only reflects past bugs. Mitigation: keep the seed set archetype-shaped (one scenario per archetype), require justification in PR descriptions for new scenarios.
- **`--full` mode brittleness:** subprocess + venv resolution can fail on contributor machines. Mitigation: opt-in via env var, same pattern we already use for the real-Claude tests.
- **Fixture realism:** if the fixture is too minimal, archetypes won't actually form. Mitigation: pad each fixture to the smallest size that yields the target archetypes (verified by inspecting the checked-in `.chameleon/profile.json`).

## Out of scope (future)

- Auto-generating scenarios from real-repo activity logs.
- Mutation testing ("would this file have been classified differently if archetype X were renamed?").
- Browser-based diff UI for refresh-script output.

## Open questions

None at design time. Open during implementation:

- Exact size of each fixture repo needed to form ~4 archetypes reliably (will be empirical).
- Whether `_meta/expected_fail.json` should also live under language buckets, or stay top-level only.
