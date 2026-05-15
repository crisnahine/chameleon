# tests/dogfood

End-to-end harness that exercises chameleon's full lifecycle: install, init, profile write, trust, injection, hook routing, drift detection, suppression, and uninstall. Scenarios run against ephemeral temp dirs and optionally against real TS or Rails repos pointed to via env vars.

Slash command: `/chameleon-dogfood` (`skills/chameleon-dogfood/SKILL.md`).

## Quick start

From the chameleon repo root:

```bash
mcp/.venv/bin/python -m tests.dogfood.runner
```

Runs all `free` + `cheap` scenarios (50 scenarios, ~$0.96 ceiling). No real Claude calls, no money spent.

## All flags

| Flag | Default | What it does |
|---|---|---|
| `--cost free,cheap` | `free,cheap` | Cost bands to include |
| `--phase 1.1` | all | Filter by phase id; `1.x` matches all 1.x |
| `--family init,trust` | all | Filter by family name |
| `--include-real-claude` | off | Allow `needs_claude=True` scenarios |
| `--include-expensive` | off | Allow `expensive` cost band |
| `--list` | off | Print matching scenarios, then exit 0 |
| `--results-dir DIR` | `tests/dogfood/results/` | Where to write JSON + MD output |
| `--max-budget-usd N` | `5.0` | Abort before run if estimated cost exceeds N |

Examples:

```bash
# List what the default run would touch
mcp/.venv/bin/python -m tests.dogfood.runner --list

# One phase only
mcp/.venv/bin/python -m tests.dogfood.runner --phase 3.x

# One family only
mcp/.venv/bin/python -m tests.dogfood.runner --family trust

# Full suite including real-Claude scenarios
mcp/.venv/bin/python -m tests.dogfood.runner --cost free,cheap,moderate --include-real-claude

# Save results somewhere else
mcp/.venv/bin/python -m tests.dogfood.runner --results-dir /tmp/dogfood-run
```

## Cost table

| Band | Count | Per-scenario ceiling | Band ceiling |
|---|---|---|---|
| `free` | 2 | $0.00 | $0.00 |
| `cheap` | 48 | $0.02 | $0.96 |
| `moderate` | 8 | $0.20 | $1.60 |
| `expensive` | 0 | $1.00 | $0.00 |
| **Total (all bands)** | **58** | | **~$2.56** |

Default run (free + cheap): 50 scenarios, $0.96 ceiling.
Default + moderate + real-Claude: 58 scenarios, ~$2.56 ceiling.

## Output

- **Stderr**: `[RUN] <id> <name>` per scenario start; `[PASS|FAIL|SKIP|ERROR] <id> <name> (<duration>s)` per end; summary table at the bottom.
- **Stdout**: machine-readable JSON array of all results (redirect to a file or discard).
- **Files**: `tests/dogfood/results/dogfood_<timestamp>.json` and `.md` (both gitignored).

Capture everything:

```bash
mcp/.venv/bin/python -m tests.dogfood.runner 2>&1 | tee /tmp/run.txt
```

## Prerequisites

Set in `.env` (gitignored):

```
CHAMELEON_TEST_TS_REPO=/abs/path/to/typescript/repo
CHAMELEON_TEST_RUBY_REPO=/abs/path/to/rails/repo
```

Without these, scenarios that require real repos skip with `SKIP (missing repo ts)`. The `free` and most `cheap` scenarios do not need them.

## Adding a scenario

1. Pick or create `tests/dogfood/scenarios/<family>.py`.
2. Write a `_run_*` function that accepts `Context` and returns `Result`.
3. Append a `Scenario(...)` entry to the file's `SCENARIOS` list.

Minimal example in `scenarios/trust.py`:

```python
def _run_trust_idempotent(ctx: Context) -> Result:
    """Re-trusting a profile that's already trusted should be a no-op."""
    profile_dir = ctx.plugin_data_dir / "profile"
    profile_dir.mkdir(parents=True)
    (profile_dir / "profile.json").write_text('{"version":1}')
    # ... exercise the tool, check the result ...
    return Result(status="PASS")

SCENARIOS = [
    # ... existing entries ...
    Scenario(
        id="3.9",
        name="trust idempotent",
        family="trust",
        needs_claude=False,
        cost="cheap",
        requires=[],          # no env vars or repos needed
        run=_run_trust_idempotent,
    ),
]
```

Field reference (from `tests/dogfood/scenario.py`):

| Field | Type | Notes |
|---|---|---|
| `id` | `str` | Dotted, e.g. `"3.9"`. Must be unique. |
| `name` | `str` | Short description shown in output. |
| `family` | `str` | Groups related scenarios; matches `--family`. |
| `needs_claude` | `bool` | `True` = requires `--include-real-claude`. |
| `cost` | `CostBand` | `free`, `cheap`, `moderate`, or `expensive`. |
| `requires` | `list[str]` | `["repo:ts"]`, `["env:MY_VAR"]`, `["fixture:path"]`. |
| `setup` | `Callable \| None` | Runs before `run`; use for shared fixture prep. |
| `teardown` | `Callable \| None` | Runs after `run` even on failure. |
| `run` | `Callable` | Main test body; returns `Result`. |

## Reading results

Each scenario ends with one of:

| Status | Meaning |
|---|---|
| `PASS` | Assertion satisfied, behavior correct. |
| `FAIL` | Assertion failed -- behavior wrong. Check `notes` for details. |
| `SKIP` | Prerequisite missing (repo env var unset, `--include-real-claude` not passed, etc.). Not a problem unless unexpected. |
| `ERROR` | Scenario raised an unhandled exception. Always a bug -- fix before merging. |

The markdown results file has a `notes` column. A `PASS (CONCERN: ...)` note means the assertion passed but the runner observed something worth watching -- not a blocker, but worth a look.

Exit code 0 = all non-skipped scenarios passed. Non-zero = at least one FAIL or ERROR.
