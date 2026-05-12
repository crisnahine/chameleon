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
      ts/
        01-utility-export.json
        02-react-component.json
        ...
      ruby/
        01-active-record-model.json
        02-controller-action.json
        ...
    runner_test.py          (unit tests for the runner itself, incl. self-test)
  fixtures/
    eval_repos/
      ts_minimal/
        .chameleon/        (entire dir checked in: COMMITTED sentinel,
                            profile.json, canonicals.json, rules.json,
                            anything else bootstrap emits)
        src/utils/example_util.ts
        src/components/ExampleComponent.tsx
        src/utils/example_util.test.ts
        package.json
      ruby_minimal/
        .chameleon/        (entire dir checked in incl. COMMITTED sentinel)
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

**Determinism of witness selection:** chameleon's canonical builder weights files inside `_RECENCY_WINDOW_SECONDS` by mtime (`canonical.py:101`). To keep refresh output stable across days and machines, the refresh script passes a pinned `now` (the fixture's commit-frozen timestamp) all the way down to `select_canonicals`. The seam exists at `canonical.py:152` (`select_canonicals(..., now=None)`) but is not currently threaded through `bootstrap_repo` (`tools.py:2030`) or `orchestrator._build_canonicals` (`orchestrator.py:1286`). **Prerequisite plumbing change**: add optional `now: float | None = None` parameter to `bootstrap_repo` / `refresh_repo` / `orchestrator._build_canonicals`, default `None`, threaded to `select_canonicals`. Land before the refresh script.

Fixtures should also be small enough that each archetype has one obvious witness (no two files of equal path length / depth inside the same archetype bucket).

**Refresh:** `scripts/refresh_eval_fixtures.sh` re-bootstraps both fixture repos in place with a pinned `now` and re-runs the scenario suite so the maintainer can review the expected-vs-new diff. Conventions: `#!/usr/bin/env bash` + `set -euo pipefail` matching `scripts/bump-version.sh`. Subcommand-style flags: `--check` (default, dry-run, exits non-zero if regeneration would change files) and `--apply` (writes). Run intentionally on profile schema bumps, not in CI.

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

- `fixture_repo`: which fixture under `tests/fixtures/eval_repos/` to use. `null` means "synthesize a no-profile repo inline": runner writes only `file_content` to a fresh tmpdir (no `.chameleon/`, no fixture copy).
- `file_path`: path relative to the (real or synthesized) repo root. Runner writes `file_content` here before the MCP call and removes it after.
- `trust_state`: one of `"trusted" | "untrusted" | "stale" | "n/a"`. Runner sets state before the call (see runner steps). `"n/a"` matches scenarios where `find_repo_root` returns None (no repo markers), per `mcp/chameleon_mcp/_constants.py:51`.
- `expected.archetype_name`: matches `result["data"]["archetype"]["archetype"]` (the cluster name, nested under `archetype`). `null` means negative scenario, no archetype should match.
- `_includes` lists are AND'd. Empty list means no assertion for that field. Match is a substring check on the relevant text, run AFTER `sanitize_for_chameleon_context` (in `mcp/chameleon_mcp/sanitization.py`) has rewritten the canonical excerpt and idioms (so the substring must be sanitizer-safe; no `<chameleon-context>`-style markers).
- `rules` is returned as a list of `[key, value]` pairs (`tools.py:901`). `rules_must_include_substring` matches against `f"{key}: {value}"` for each pair. `idioms_must_include_substring` matches against the idioms text blob.

### Runner modes

```bash
# MCP layer (default; every CI run)
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/hook_evals/runner.py

# Full hook plumbing (opt-in via CLI flag)
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/hook_evals/runner.py --full
```

**Scenario discovery (both modes):** runner does `sorted(glob('tests/hook_evals/scenarios/**/*.json', recursive=True))`. Single exit code across the full set. Language is inferred from the path segment (`scenarios/ts/...`, `scenarios/ruby/...`) for grouped reporting; no separate exit codes per language.

**Default (MCP-layer) mode** per scenario:

1. Allocate a new `tempfile.TemporaryDirectory`. Call its path `<repo_tmp>`. If `fixture_repo` is non-null, copy the named fixture into `<repo_tmp>`. If `fixture_repo` is null, the runner synthesizes a minimal repo: write an empty `package.json` (TS file_path) or `Gemfile` (Ruby file_path) at `<repo_tmp>/`, no `.chameleon/`. The marker file gives `find_repo_root` something to find so the response has `profile_status: "no_profile"` (per `loader.py:60-130`). Without the marker, the response is `profile_status: "no_repo"` instead — use that explicitly if a scenario wants the no_repo path.
2. Allocate a second tmpdir for plugin data, set `CHAMELEON_PLUGIN_DATA=<data_tmp>` for the duration of the scenario. Isolates trust state, index db, drift db, and `hook_helper`'s marker files. Note: the bash hook script's error log path is hardcoded to `${HOME}/.local/share/chameleon/.hook_errors.log` (see `hooks/preflight-and-advise:35-37`) and is NOT redirected by this variable. That only matters in `--full` mode (see below).
3. Apply `trust_state`:
   - `untrusted`: do nothing. Fresh `CHAMELEON_PLUGIN_DATA` means no trust record.
   - `trusted`: call `trust_profile(<repo_tmp>, confirmation_token=<repo_tmp basename>)`. Basename satisfies the token check in `tools.py:2676`.
   - `stale`: call `trust_profile`, then append a single byte (e.g. a space) to any of the 5 hashed artifacts in `<repo_tmp>/.chameleon/` (`archetypes.json`, `canonicals.json`, `idioms.md`, `profile.json`, `rules.json` per `trust.py:135-141`). Recompute on next `get_pattern_context` call will yield `is_material_change == true` and surface `trust_state: "stale"`.
   - `n/a`: only valid when `fixture_repo: null` AND the runner is synthesizing the no_repo path (no marker file). Skip the trust call.
4. Write `file_content` to `<repo_tmp>/<file_path>`.
5. Call `chameleon_mcp.tools.get_pattern_context(<repo_tmp>/<file_path>)` in-process.
6. Inspect `result["data"]["repo"]["profile_status"]`. If `"profile_corrupted"`, emit a `SCHEMA_ROT` failure that tells the contributor: "fixture profile is unloadable, run `scripts/refresh_eval_fixtures.sh` to regenerate." Do not chase advisory mismatches in this case. (Note: `get_pattern_context` does NOT return `"profile_unsupported_schema_version"` — that status only appears via `get_rules`. `profile_corrupted` is the only schema-rot signal here.)
7. Otherwise, assert against `expected`.
8. Both tmpdirs auto-removed.

**`--full` mode** per scenario: same steps 1-4, then:

5. Capability check (once, before first scenario): verify `hooks/preflight-and-advise` is executable, `bash` is on PATH, and `mcp/.venv/bin/python -m chameleon_mcp.hook_helper --help` exits zero. If any fails, abort `--full` mode with a clear "skipped, capability missing" message. No per-scenario fail.
6. Record mtime of `${HOME}/.local/share/chameleon/.hook_errors.log` if it exists, else None. (We watch the REAL log; the bash script doesn't honor `CHAMELEON_PLUGIN_DATA`. `--full` mode therefore appends to the developer's real error log on hook failure — documented cost.)
7. Build a PreToolUse event JSON: `{"tool_name": "Edit", "tool_input": {"file_path": "<repo_tmp>/<file_path>"}, "session_id": "hook_evals"}`. `tool_name` is decorative in this path (`hook_helper.py:148-154` reads only `tool_input.file_path` and `session_id`); include for shape fidelity. Pipe through `hooks/preflight-and-advise`, capture stdout. Subprocess stderr should also be captured for human diagnostics on failure.
8. If `${HOME}/.local/share/chameleon/.hook_errors.log` grew during the call, the hook fail-opened. FAIL the scenario as `HOOK_FAILED` regardless of the assertions.
9. Parse the hook's stdout JSON. Read advisory from `hookSpecificOutput.additionalContext` if present, else top-level `additionalContext` (the helper emits both defensively, per `hook_helper.py:80-96`). Assert against `expected`.

Catches hook script regressions (`run-hook.cmd`, venv resolution, timeout wrapper, error logging) that MCP-layer mode skips.

**Exit + output contract.** Runner exits 0 if all scenarios pass, non-zero if any fail. Stdout: a single pretty-printed JSON document (one object, multi-line) mirroring `tests/calibration/harness.py:26-27`. Stderr: per-scenario PASS/FAIL lines for human reading. Prints a final line starting with `Summary` so `tests/run_all_orders.py:54-56` can grep-trim output for its summary block.

### Self-test

The runner's own logic is tested by `tests/hook_evals/runner_test.py` (a standard Python test, runs as part of `run_all_orders.py`). The unit test:

1. Constructs an in-memory scenario object whose `expected` block cannot match any healthy run (e.g. `archetype_name: "definitely_not_real_archetype"`).
2. Calls the runner's assertion function directly with a known `get_pattern_context` response.
3. Asserts the function returns at least one mismatch.

This is a normal unit test, no inversion logic, no special-case path in the scenario corpus. Trade-off accepted: it doesn't exercise the JSON loader / file-discovery layer, which is instead covered by parametric "load a real scenario file and check schema" assertions in the same test file.

### CI integration

Add MCP-mode run to `tests/run_all_orders.py` as a 6th entry. Soft runtime target: under 5 seconds across ~10 MCP-layer scenarios on a warm cache. Measure during implementation; if `--full` mode lands inside CI later it will need its own (larger) budget because of per-scenario Python startup.

`--full` mode is opt-in via the CLI flag only, no env var. Capability-based skip inside `--full` (bash, hook executable, hook_helper importable) follows the existing pattern in `pretooluse_hook_test.py:38` (`shutil.which("claude") is None` → skip) and `tests/calibration/harness.py:31-47` (missing `corpus.json` → `_no_corpus()`). Same gating shape, capability-driven.

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

- `trust_state: "untrusted"`: advisory still returned, trust flag surfaced.
- `trust_state: "stale"`: profile drift surfaced after the in-tmpdir hash bump described in runner step 3.
- Repo missing `.chameleon/` but with a language marker: scenario uses `fixture_repo: null` with a TS or Ruby `file_path`. Runner drops an empty `package.json` or `Gemfile` marker (depending on extension) so `find_repo_root` resolves. Expected: `profile_status == "no_profile"`, archetype null, no canonical, `trust_state` per `tools.py` (`"untrusted"` when profile is absent).
- Repo with no markers at all (rare): same scenario shape with `expected.profile_status == "no_repo"`, `trust_state == "n/a"`.

## Risks

- **Fixture profile rot:** chameleon's profile schema evolves. When `PROFILE_SCHEMA_VERSION` bumps or a field is renamed, fixture profiles become unloadable. The loader raises `ProfileLoadError`, which surfaces as `profile_status: "profile_corrupted"` from `get_pattern_context`. Mitigation: runner detects this and emits a dedicated `SCHEMA_ROT` failure with the refresh command, instead of letting it look like an advisory regression.
- **Witness-selection non-determinism:** mtime weighting in `canonical.py` makes witness selection date-dependent if multiple files tie on other criteria. Mitigation: refresh script pins `now`; fixtures use single-witness archetypes (one canonical-eligible file per bucket). NOTE: pinning requires a small prerequisite change to thread `now=` through `bootstrap_repo` / `refresh_repo` / `orchestrator._build_canonicals` to reach `canonical.select_canonicals`'s existing `now` parameter (`canonical.py:152`). Implementation plan must include this plumbing change.
- **Plugin-data dir pollution (MCP mode):** without isolation, every scenario writes trust/index/drift state into `~/.local/share/chameleon/<random_repo_id>/`. Mitigation: runner sets `CHAMELEON_PLUGIN_DATA` per scenario to a tmpdir.
- **`--full` mode appends to real error log:** the bash hook scripts hardcode `LOG_DIR="${HOME}/.local/share/chameleon"` (`preflight-and-advise:35`, also `posttool-recorder`, `session-start`, `callout-detector`). `CHAMELEON_PLUGIN_DATA` cannot redirect that. `--full` mode runs the real bash hook, so any hook failure appends one line to the developer's actual error log. Acceptable cost given `--full` is opt-in and the log is append-only; runner uses this same log to detect fail-open via mtime watch.
- **`--full` mode silent fail-open:** the hook prints `{}` on any failure; negative scenarios (`archetype: null`) would falsely pass. Mitigation: capability check up-front + per-scenario mtime watch on the real `.hook_errors.log`.
- **Overfitting:** every regression PR could add a scenario to silence its own failure. The corpus then only reflects past bugs. Mitigation: keep the seed set archetype-shaped (one scenario per archetype), require justification in PR descriptions for new scenarios.
- **Fixture realism:** if the fixture is too minimal, archetypes won't actually form. Mitigation: pad each fixture to the smallest size that yields the target archetypes (verified by inspecting the checked-in `.chameleon/profile.json`).

## Out of scope (future)

- Auto-generating scenarios from real-repo activity logs.
- Mutation testing ("would this file have been classified differently if archetype X were renamed?").
- Browser-based diff UI for refresh-script output.

## Open questions

Resolved during round 1 review (recorded for traceability):

- No-profile case is handled by `fixture_repo: null` + inline synthesis, not by adding a third fixture dir.
- Runner self-test is `runner_test.py`, not a meta-scenario with inversion.

Resolved during round 2 cross-verification:

- `--full` mode opt-in is via CLI flag only, no env var (capability-based skip matches existing pattern).
- Fail-open detection watches the real `${HOME}/.local/share/chameleon/.hook_errors.log` (hardcoded in bash hooks; cannot be redirected by `CHAMELEON_PLUGIN_DATA`). Documented contamination of the developer's real log on hook failure during `--full`.
- `get_pattern_context` envelope shapes (archetype nested under `data.archetype.archetype`, rules as `[key, value]` list, `trust_state` enum includes `"n/a"`) reflected in scenario schema.
- SCHEMA_ROT branch checks only `profile_corrupted` (the only schema-rot status `get_pattern_context` actually returns).
- Pinned-`now` requires a prerequisite plumbing change to thread the param through `bootstrap_repo` / `refresh_repo` / `orchestrator`. Implementation plan owns this.

Implementation-time empirical:

- Exact size of each fixture repo needed to form 3-4 archetypes reliably (will be measured by inspecting bootstrap output).
- Whether `--full` mode runtime is acceptable for a next-tier CI gate (not on every PR, but on main).
