# Journey Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `tests/` (95+ unit tests + dogfood + e2e + hook_evals + calibration) with a single real-world journey harness that drives `claude -p` sessions through 12 acts covering 38 phases of chameleon's surface.

**Architecture:** Per-run isolation via env overrides keeps the developer's `~/.local/share/chameleon/` untouched. Each act spawns one `claude -p` subprocess with a multi-turn prompt + structured JSONL checkpoint file for phase attribution. Runner-side `expect.*` helpers verify filesystem state, not Claude prose. Mid-run cost abort stops runaway spend.

**Tech Stack:** Python 3.11+ (existing `mcp/.venv/`), `claude -p` CLI with `--output-format stream-json`, real fixture repos with loopback git origins, pytest for harness library unit tests.

**Spec:** `docs/superpowers/specs/2026-05-21-journey-harness-design.md` (624 lines, 5 rounds of expert review).

---

## File structure

```
tests/journey/
├── __init__.py
├── runner.py                       Entry point. Argparse + preflight + act orchestration + output.
├── harness/
│   ├── __init__.py
│   ├── context.py                  JourneyContext dataclass. Env setup, working dirs, time helpers.
│   ├── fixtures.py                 Copy seed fixtures to <run_dir>/working, init git + loopback origin.
│   ├── checkpoints.py              Parse JSONL checkpoint file, count parse errors, attribute phases.
│   ├── expect.py                   Assertion helpers (path_exists, json_field, hook_fired, etc.).
│   ├── snapshots.py                Capture .chameleon/ + plugin_data state per phase.
│   ├── bash.py                     subprocess.run wrapper with timeout + env.
│   ├── mcp.py                      Direct MCP stdio tool calls (runner-side instrumentation).
│   ├── claude.py                   Spawn claude -p + parse stream-json + extract hook events + cost.
│   ├── git_shim.py                 Plant fake git on PATH for timeout testing. ShimHandle context manager.
│   ├── preflight.py                Runner-side preflight: claude on PATH, fixtures present, git >= 2.28, lockfile.
│   └── tests/                      Pytest unit tests for the harness library itself.
│       ├── __init__.py
│       ├── test_checkpoints.py
│       ├── test_expect.py
│       ├── test_fixtures.py
│       └── test_claude_parser.py
├── acts/
│   ├── __init__.py
│   ├── act_base.py                 ActResult dataclass, base helpers.
│   ├── act_00_preflight.py
│   ├── act_01_install_mcp_doctor.py
│   ├── act_02_init_flow.py
│   ├── act_03_hot_path_drift.py
│   ├── act_04_v060_ux_bundle.py
│   ├── act_05_teach_status_doctor.py
│   ├── act_06_suppression_callout.py
│   ├── act_07_rails_parity.py
│   ├── act_08_hooks_security_sanitization.py
│   ├── act_09_schema_atomicity_concurrency.py
│   ├── act_10_daemon_observability_resilience.py
│   └── act_11_uninstall_cleanup.py
├── fixtures/                       Committed seed repos (source-code-only, no .git/).
│   ├── ts_basic/
│   ├── rails_basic/
│   ├── ts_monorepo/
│   └── ts_with_rails_sidecar/
└── results/                        Gitignored. Per-run output.
```

To delete during cleanup (Phase 5):
- `tests/dogfood/` (entire dir)
- `tests/e2e/` (entire dir)
- `tests/hook_evals/` (entire dir)
- `tests/calibration/` (entire dir)
- `tests/fixtures/` (entire dir, old fixtures replaced by journey/fixtures/)
- `tests/run_all_orders.py`
- All `tests/*_test.py` files (~95 files)
- `tests/_test_config.py`, `tests/test-helpers.sh`, `tests/skill_triggering_test.sh`
- `tests/__pycache__/`, `tests/__init__.py` (if present)

To rename:
- `skills/chameleon-dogfood/` → `skills/chameleon-journey/`

---

## Phase 0: Project setup

### Task 0.1: Add gitignore entries

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: Read current .gitignore**

Run: `cat .gitignore`

- [ ] **Step 2: Append journey results + cache entries**

Add these lines at the bottom of `.gitignore`:

```
# Journey harness run output (per-run ephemeral dirs)
tests/journey/results/
# Pytest cache for harness library tests
tests/journey/harness/tests/__pycache__/
tests/journey/harness/tests/.pytest_cache/
```

- [ ] **Step 3: Verify**

Run: `git check-ignore -v tests/journey/results/sample`
Expected: `.gitignore:<line>:tests/journey/results/ tests/journey/results/sample`

- [ ] **Step 4: Commit**

```bash
git add .gitignore
git commit -m "Add gitignore entries for journey harness results"
```

### Task 0.2: Create directory skeleton

**Files:**
- Create: `tests/journey/__init__.py`
- Create: `tests/journey/harness/__init__.py`
- Create: `tests/journey/harness/tests/__init__.py`
- Create: `tests/journey/acts/__init__.py`
- Create: `tests/journey/fixtures/.gitkeep`

- [ ] **Step 1: Create dirs + init files**

```bash
mkdir -p tests/journey/{harness/tests,acts,fixtures}
touch tests/journey/__init__.py
touch tests/journey/harness/__init__.py
touch tests/journey/harness/tests/__init__.py
touch tests/journey/acts/__init__.py
touch tests/journey/fixtures/.gitkeep
```

- [ ] **Step 2: Verify structure**

Run: `find tests/journey -type f | sort`
Expected: 5 files listed (the 4 __init__.py + .gitkeep).

- [ ] **Step 3: Commit**

```bash
git add tests/journey/
git commit -m "Add journey harness directory skeleton"
```

---

## Phase 1: Harness library

TDD for the tricky pieces (checkpoint parser, claude stream-json parser, expect helpers, fixture setup). Glue code without unit tests is fine where the logic is trivial.

### Task 1.1: harness/context.py (JourneyContext)

**Files:**
- Create: `tests/journey/harness/context.py`
- Test: `tests/journey/harness/tests/test_context.py`

- [ ] **Step 1: Write failing test**

Create `tests/journey/harness/tests/test_context.py`:

```python
"""Unit tests for JourneyContext."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.journey.harness.context import JourneyContext, build_context


def test_build_context_creates_run_dir(tmp_path: Path) -> None:
    """build_context must create the run directory and all subdirs."""
    plugin_root = tmp_path / "chameleon"
    plugin_root.mkdir()
    results_root = tmp_path / "results"

    ctx = build_context(plugin_root, results_root)

    assert ctx.run_dir.exists()
    assert ctx.run_dir.parent == results_root
    assert (ctx.run_dir / "chameleon_data").exists()
    assert (ctx.run_dir / "tmp").exists()
    assert (ctx.run_dir / "working").exists()
    assert (ctx.run_dir / "checkpoints").exists()
    assert (ctx.run_dir / "transcripts").exists()
    assert (ctx.run_dir / "snapshots").exists()


def test_env_overrides_point_under_run_dir(tmp_path: Path) -> None:
    """All four env overrides must point under the run_dir."""
    plugin_root = tmp_path / "chameleon"
    plugin_root.mkdir()
    results_root = tmp_path / "results"

    ctx = build_context(plugin_root, results_root)

    assert ctx.env["CHAMELEON_PLUGIN_DATA"].startswith(str(ctx.run_dir))
    assert ctx.env["CHAMELEON_HMAC_KEY_PATH"].startswith(str(ctx.run_dir))
    assert ctx.env["TMPDIR"].startswith(str(ctx.run_dir))
    assert ctx.env["CHAMELEON_HOOK_ERROR_LOG"].startswith(str(ctx.run_dir))


def test_fast_forward_marker_ages_mtime(tmp_path: Path) -> None:
    """fast_forward_marker must rewind both atime and mtime."""
    plugin_root = tmp_path / "chameleon"
    plugin_root.mkdir()
    ctx = build_context(plugin_root, tmp_path / "results")

    marker = tmp_path / "marker.txt"
    marker.write_text("hi")

    ctx.fast_forward_marker(marker, age_seconds=3600)

    age = ctx.now() - marker.stat().st_mtime
    assert age >= 3600, f"expected mtime aged >= 3600s, got {age}"
```

- [ ] **Step 2: Run test (verify it fails)**

Run: `cd /Users/crisn/Documents/Projects/chameleon && PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/journey/harness/tests/test_context.py -v`
Expected: FAIL with "No module named 'tests.journey.harness.context'".

- [ ] **Step 3: Write context.py**

Create `tests/journey/harness/context.py`:

```python
"""JourneyContext: shared state for the journey harness.

Per-run isolation: all chameleon state writes go to <run_dir>/chameleon_data
via CHAMELEON_PLUGIN_DATA. HMAC key, exec log, hook errors log are also
per-run-dir to keep the developer's home dir untouched.
"""
from __future__ import annotations

import dataclasses
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclasses.dataclass
class JourneyContext:
    plugin_root: Path
    run_dir: Path
    plugin_data_dir: Path
    hmac_key_path: Path
    tmpdir: Path
    hook_error_log: Path
    env: dict[str, str]
    cost_so_far_usd: float = 0.0
    current_checkpoint_file: Path | None = None
    fixtures: dict[str, Path] = dataclasses.field(default_factory=dict)
    origins: dict[str, Path] = dataclasses.field(default_factory=dict)
    act_results: list[Any] = dataclasses.field(default_factory=list)

    def now(self) -> float:
        return time.time()

    def fast_forward_marker(self, path: Path, age_seconds: int) -> None:
        """Set atime + mtime to (now - age_seconds). Simulates aged file."""
        target = self.now() - age_seconds
        os.utime(path, (target, target))

    def fixture(self, name: str) -> Path:
        if name not in self.fixtures:
            raise KeyError(f"fixture {name!r} not registered; available: {sorted(self.fixtures)}")
        return self.fixtures[name]

    def origin(self, name: str) -> Path:
        if name not in self.origins:
            raise KeyError(f"origin {name!r} not registered; available: {sorted(self.origins)}")
        return self.origins[name]

    def projected_remaining_cost(self, remaining_act_ceilings: list[float]) -> float:
        return self.cost_so_far_usd + sum(remaining_act_ceilings)


def build_context(plugin_root: Path, results_root: Path) -> JourneyContext:
    """Create a new run_dir with all subdirs and env overrides."""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = results_root / f"journey_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=False)

    for sub in ("chameleon_data", "tmp", "working", "checkpoints", "transcripts", "snapshots"):
        (run_dir / sub).mkdir()

    plugin_data_dir = run_dir / "chameleon_data"
    hmac_key_path = run_dir / "exec_hmac.key"
    tmpdir = run_dir / "tmp"
    hook_error_log = run_dir / "hook_errors.log"

    env = {
        "CHAMELEON_PLUGIN_DATA": str(plugin_data_dir),
        "CHAMELEON_HMAC_KEY_PATH": str(hmac_key_path),
        "TMPDIR": str(tmpdir),
        "CHAMELEON_HOOK_ERROR_LOG": str(hook_error_log),
    }

    return JourneyContext(
        plugin_root=plugin_root,
        run_dir=run_dir,
        plugin_data_dir=plugin_data_dir,
        hmac_key_path=hmac_key_path,
        tmpdir=tmpdir,
        hook_error_log=hook_error_log,
        env=env,
    )
```

- [ ] **Step 4: Run test (verify it passes)**

Run: `cd /Users/crisn/Documents/Projects/chameleon && PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/journey/harness/tests/test_context.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/journey/harness/context.py tests/journey/harness/tests/test_context.py
git commit -m "Add JourneyContext + build_context helper"
```

### Task 1.2: harness/checkpoints.py (parse JSONL, attribute phases)

**Files:**
- Create: `tests/journey/harness/checkpoints.py`
- Test: `tests/journey/harness/tests/test_checkpoints.py`

- [ ] **Step 1: Write failing tests**

Create `tests/journey/harness/tests/test_checkpoints.py`:

```python
"""Unit tests for checkpoint JSONL parsing + phase attribution."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.journey.harness.checkpoints import (
    PhaseOutcome,
    parse_checkpoint_file,
)


def test_started_and_completed_pair(tmp_path: Path) -> None:
    """Phase with both started + completed events is PASS."""
    f = tmp_path / "cp.jsonl"
    f.write_text(
        '{"phase": 1, "status": "started", "ts": "2026-05-21T00:00:00Z"}\n'
        '{"phase": 1, "status": "completed", "ts": "2026-05-21T00:00:05Z"}\n'
    )

    outcomes, parse_errors = parse_checkpoint_file(f, expected_phases=[1])

    assert parse_errors == 0
    assert outcomes[1].status == "PASS"


def test_started_without_completed_is_fail(tmp_path: Path) -> None:
    """Phase that started but didn't complete is FAIL."""
    f = tmp_path / "cp.jsonl"
    f.write_text('{"phase": 2, "status": "started", "ts": "2026-05-21T00:00:00Z"}\n')

    outcomes, parse_errors = parse_checkpoint_file(f, expected_phases=[2])

    assert parse_errors == 0
    assert outcomes[2].status == "FAIL"
    assert "incomplete" in outcomes[2].notes.lower()


def test_phase_never_started_is_skip(tmp_path: Path) -> None:
    """Expected phase missing entirely is SKIP."""
    f = tmp_path / "cp.jsonl"
    f.write_text("")

    outcomes, parse_errors = parse_checkpoint_file(f, expected_phases=[5])

    assert parse_errors == 0
    assert outcomes[5].status == "SKIP"
    assert "not attempted" in outcomes[5].notes.lower()


def test_explicit_failed_status(tmp_path: Path) -> None:
    """Phase with status:failed event is FAIL."""
    f = tmp_path / "cp.jsonl"
    f.write_text(
        '{"phase": 3, "status": "started", "ts": "2026-05-21T00:00:00Z"}\n'
        '{"phase": 3, "status": "failed", "ts": "2026-05-21T00:00:02Z", "notes": "assertion X"}\n'
    )

    outcomes, parse_errors = parse_checkpoint_file(f, expected_phases=[3])

    assert outcomes[3].status == "FAIL"
    assert "assertion X" in outcomes[3].notes


def test_malformed_line_is_skipped(tmp_path: Path) -> None:
    """Malformed JSON line increments parse_errors but doesn't abort."""
    f = tmp_path / "cp.jsonl"
    f.write_text(
        '{"phase": 4, "status": "started", "ts": "2026-05-21T00:00:00Z"}\n'
        'this is not json\n'
        '{"phase": 4, "status": "completed", "ts": "2026-05-21T00:00:05Z"}\n'
    )

    outcomes, parse_errors = parse_checkpoint_file(f, expected_phases=[4])

    assert parse_errors == 1
    assert outcomes[4].status == "PASS"


def test_skip_phase_with_parse_errors_includes_hint(tmp_path: Path) -> None:
    """SKIP-attributed phases include corruption hint when parse_errors > 0."""
    f = tmp_path / "cp.jsonl"
    f.write_text(
        'invalid json line\n'
        '{"phase": 5, "status": "started", "ts": "2026-05-21T00:00:00Z"}\n'
        '{"phase": 5, "status": "completed", "ts": "2026-05-21T00:00:05Z"}\n'
    )

    outcomes, parse_errors = parse_checkpoint_file(f, expected_phases=[5, 6])

    assert parse_errors == 1
    assert outcomes[5].status == "PASS"
    assert outcomes[6].status == "SKIP"
    assert "checkpoint corruption" in outcomes[6].notes.lower()
```

- [ ] **Step 2: Run test (verify it fails)**

Run: `cd /Users/crisn/Documents/Projects/chameleon && PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/journey/harness/tests/test_checkpoints.py -v`
Expected: FAIL with "No module named 'tests.journey.harness.checkpoints'".

- [ ] **Step 3: Write checkpoints.py**

Create `tests/journey/harness/checkpoints.py`:

```python
"""Parse JSONL checkpoint file emitted by Claude inside an act session.

Schema per line:
  {"phase": <int>, "status": "started"|"completed"|"failed", "ts": "<ISO 8601>", "notes": "<optional>"}

Malformed lines are logged (caller decides where) and skipped via a parse-error count.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Literal

StatusName = Literal["PASS", "FAIL", "SKIP", "ERROR"]


@dataclasses.dataclass
class PhaseOutcome:
    phase: int
    status: StatusName
    notes: str = ""
    started_ts: str | None = None
    completed_ts: str | None = None


def parse_checkpoint_file(
    path: Path, expected_phases: list[int]
) -> tuple[dict[int, PhaseOutcome], int]:
    """Parse a checkpoint JSONL file and attribute phase outcomes.

    Returns (outcomes, parse_errors_count).

    Behavior:
      - Malformed JSON line → increments parse_errors, skipped.
      - Phase with started + completed → PASS.
      - Phase with started + failed → FAIL.
      - Phase with started only → FAIL "phase incomplete (no completion event)".
      - Expected phase with no events → SKIP "phase not attempted (likely upstream failure)".
      - When parse_errors > 0, SKIP-attributed phases get an extra corruption hint.
    """
    events: dict[int, list[dict]] = {}
    parse_errors = 0

    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            phase = obj.get("phase")
            if not isinstance(phase, int):
                parse_errors += 1
                continue
            events.setdefault(phase, []).append(obj)

    outcomes: dict[int, PhaseOutcome] = {}
    for phase in expected_phases:
        phase_events = events.get(phase, [])
        if not phase_events:
            note = "phase not attempted (likely upstream failure)"
            if parse_errors > 0:
                note += " (may be checkpoint corruption, check transcripts)"
            outcomes[phase] = PhaseOutcome(phase=phase, status="SKIP", notes=note)
            continue

        started = next((e for e in phase_events if e.get("status") == "started"), None)
        completed = next((e for e in phase_events if e.get("status") == "completed"), None)
        failed = next((e for e in phase_events if e.get("status") == "failed"), None)

        if failed:
            outcomes[phase] = PhaseOutcome(
                phase=phase,
                status="FAIL",
                notes=failed.get("notes", "explicit failed status"),
                started_ts=started.get("ts") if started else None,
                completed_ts=failed.get("ts"),
            )
        elif started and completed:
            outcomes[phase] = PhaseOutcome(
                phase=phase,
                status="PASS",
                notes=completed.get("notes", ""),
                started_ts=started.get("ts"),
                completed_ts=completed.get("ts"),
            )
        elif started and not completed:
            outcomes[phase] = PhaseOutcome(
                phase=phase,
                status="FAIL",
                notes="phase incomplete (no completion event)",
                started_ts=started.get("ts"),
            )
        else:
            outcomes[phase] = PhaseOutcome(
                phase=phase,
                status="FAIL",
                notes="unexpected event sequence (no started)",
            )

    return outcomes, parse_errors
```

- [ ] **Step 4: Run tests (verify they pass)**

Run: `cd /Users/crisn/Documents/Projects/chameleon && PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/journey/harness/tests/test_checkpoints.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/journey/harness/checkpoints.py tests/journey/harness/tests/test_checkpoints.py
git commit -m "Add checkpoint JSONL parser with parse-error tolerance"
```

### Task 1.3: harness/bash.py (subprocess wrapper)

**Files:**
- Create: `tests/journey/harness/bash.py`
- Test: `tests/journey/harness/tests/test_bash.py`

- [ ] **Step 1: Write failing test**

Create `tests/journey/harness/tests/test_bash.py`:

```python
"""Unit tests for bash subprocess wrapper."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.journey.harness.bash import BashResult, run_bash


def test_basic_command_capture(tmp_path: Path) -> None:
    """Run echo, capture stdout."""
    result = run_bash("echo hello", cwd=tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == "hello"


def test_env_override(tmp_path: Path) -> None:
    """Env dict overrides process env."""
    result = run_bash("printenv MY_TEST_VAR", cwd=tmp_path, env={"MY_TEST_VAR": "abc"})
    assert result.stdout.strip() == "abc"


def test_timeout(tmp_path: Path) -> None:
    """Timeout raises BashTimeout."""
    from tests.journey.harness.bash import BashTimeout

    with pytest.raises(BashTimeout):
        run_bash("sleep 5", cwd=tmp_path, timeout_s=1)


def test_non_zero_exit(tmp_path: Path) -> None:
    """Non-zero exit is captured, not raised."""
    result = run_bash("false", cwd=tmp_path)
    assert result.returncode == 1
```

- [ ] **Step 2: Run test (verify it fails)**

Run: `cd /Users/crisn/Documents/Projects/chameleon && PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/journey/harness/tests/test_bash.py -v`
Expected: FAIL with module not found.

- [ ] **Step 3: Write bash.py**

Create `tests/journey/harness/bash.py`:

```python
"""Bash subprocess wrapper used by the runner (and acts) for filesystem setup.

Distinct from Claude's own Bash tool calls inside a session.
"""
from __future__ import annotations

import dataclasses
import os
import subprocess
from pathlib import Path


class BashTimeout(Exception):
    pass


@dataclasses.dataclass
class BashResult:
    returncode: int
    stdout: str
    stderr: str


def run_bash(
    command: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout_s: int = 30,
) -> BashResult:
    """Run a bash command, capture output. Inherits + overrides env."""
    merged_env = os.environ.copy()
    if env is not None:
        merged_env.update(env)
    try:
        proc = subprocess.run(
            ["bash", "-c", command],
            cwd=str(cwd) if cwd else None,
            env=merged_env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BashTimeout(f"bash command exceeded {timeout_s}s: {command!r}") from exc

    return BashResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/crisn/Documents/Projects/chameleon && PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/journey/harness/tests/test_bash.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/journey/harness/bash.py tests/journey/harness/tests/test_bash.py
git commit -m "Add bash subprocess wrapper with timeout + env override"
```

### Task 1.4: harness/fixtures.py (copy seeds, setup git, loopback origin)

**Files:**
- Create: `tests/journey/harness/fixtures.py`
- Test: `tests/journey/harness/tests/test_fixtures.py`

- [ ] **Step 1: Write failing test**

Create `tests/journey/harness/tests/test_fixtures.py`:

```python
"""Unit tests for fixture setup + loopback origin."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.journey.harness.bash import run_bash
from tests.journey.harness.fixtures import (
    GitVersionError,
    check_git_version,
    setup_fixture,
)


def test_check_git_version_accepts_recent() -> None:
    """check_git_version returns the parsed version tuple on >= 2.28."""
    major, minor = check_git_version(min_version=(2, 28))
    assert major >= 2
    if major == 2:
        assert minor >= 28


def test_setup_fixture_copies_and_inits(tmp_path: Path) -> None:
    """setup_fixture copies seed, runs git init, sets up loopback origin."""
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "hello.txt").write_text("hi\n")

    working_root = tmp_path / "working"
    working_root.mkdir()

    work_dir, origin_dir = setup_fixture("myfix", seed, working_root)

    # Working copy has the seed content
    assert (work_dir / "hello.txt").read_text() == "hi\n"
    # Working copy is a git repo on branch 'main'
    result = run_bash("git branch --show-current", cwd=work_dir)
    assert result.stdout.strip() == "main"
    # origin/main is reachable
    result = run_bash("git show origin/main:hello.txt", cwd=work_dir)
    assert result.stdout == "hi\n"


def test_setup_fixture_origin_is_bare(tmp_path: Path) -> None:
    """The loopback origin is a bare repo."""
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "file.txt").write_text("x\n")

    work_dir, origin_dir = setup_fixture("myfix", seed, tmp_path / "working")

    assert origin_dir.name.endswith(".git")
    # Bare repos have no working tree
    assert not (origin_dir / "file.txt").exists()
    # But have HEAD
    assert (origin_dir / "HEAD").exists()
```

- [ ] **Step 2: Run test (verify it fails)**

Run: `cd /Users/crisn/Documents/Projects/chameleon && PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/journey/harness/tests/test_fixtures.py -v`
Expected: FAIL with module not found.

- [ ] **Step 3: Write fixtures.py**

Create `tests/journey/harness/fixtures.py`:

```python
"""Fixture setup: copy committed seed to <run_dir>/working, init git, set up loopback origin.

Committed seeds under tests/journey/fixtures/<name>/ are SOURCE-CODE-ONLY (no .git/).
This module initializes them as git repos with a bare loopback origin so
`git show origin/main:<artifact>` works offline.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from tests.journey.harness.bash import run_bash


class GitVersionError(Exception):
    pass


def check_git_version(min_version: tuple[int, int] = (2, 28)) -> tuple[int, int]:
    """Verify git --version >= min_version. Returns (major, minor)."""
    result = run_bash("git --version")
    if result.returncode != 0:
        raise GitVersionError(f"git not found: {result.stderr}")
    match = re.search(r"git version (\d+)\.(\d+)", result.stdout)
    if not match:
        raise GitVersionError(f"could not parse git version: {result.stdout!r}")
    major, minor = int(match.group(1)), int(match.group(2))
    if (major, minor) < min_version:
        raise GitVersionError(
            f"git {major}.{minor} found, but >= {min_version[0]}.{min_version[1]} required "
            f"(--initial-branch flag unavailable)"
        )
    return major, minor


def setup_fixture(name: str, seed: Path, working_root: Path) -> tuple[Path, Path]:
    """Copy seed to working_root/name, init git, set up loopback origin.

    Returns (work_dir, origin_dir).

    work_dir = working_root/name with a fresh git repo on branch 'main'.
    origin_dir = working_root/origin_<name>.git (bare clone, set as origin).
    """
    work_dir = working_root / name
    origin_dir = working_root / f"origin_{name}.git"

    # Copy seed to work_dir
    shutil.copytree(seed, work_dir)

    # Initialize git with explicit main branch
    cmds = [
        "git init --initial-branch=main -q",
        "git config user.name 'journey harness'",
        "git config user.email 'harness@journey.local'",
        "git add -A",
        "git commit -q -m 'seed'",
    ]
    for cmd in cmds:
        r = run_bash(cmd, cwd=work_dir)
        if r.returncode != 0:
            raise RuntimeError(f"fixture setup failed at {cmd!r}: {r.stderr}")

    # Create bare loopback origin
    r = run_bash(f"git clone --bare . {origin_dir}", cwd=work_dir)
    if r.returncode != 0:
        raise RuntimeError(f"bare clone failed: {r.stderr}")

    # Wire origin
    r = run_bash(f"git remote add origin {origin_dir}", cwd=work_dir)
    if r.returncode != 0:
        raise RuntimeError(f"remote add failed: {r.stderr}")
    r = run_bash("git fetch -q origin", cwd=work_dir)
    if r.returncode != 0:
        raise RuntimeError(f"git fetch failed: {r.stderr}")
    r = run_bash("git branch --set-upstream-to=origin/main main", cwd=work_dir)
    if r.returncode != 0:
        raise RuntimeError(f"upstream setup failed: {r.stderr}")

    return work_dir, origin_dir
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/crisn/Documents/Projects/chameleon && PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/journey/harness/tests/test_fixtures.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/journey/harness/fixtures.py tests/journey/harness/tests/test_fixtures.py
git commit -m "Add fixture setup with loopback git origin"
```

### Task 1.5: harness/expect.py (assertion helpers)

**Files:**
- Create: `tests/journey/harness/expect.py`
- Test: `tests/journey/harness/tests/test_expect.py`

- [ ] **Step 1: Write failing test**

Create `tests/journey/harness/tests/test_expect.py`:

```python
"""Unit tests for expect.* assertion helpers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.journey.harness import expect


def test_path_exists_passes(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("x")
    expect.path_exists(phase=1, path=f)


def test_path_exists_fails(tmp_path: Path) -> None:
    with pytest.raises(expect.PhaseAssertionError) as exc:
        expect.path_exists(phase=2, path=tmp_path / "missing.txt")
    assert "phase 2" in str(exc.value).lower()


def test_path_absent_passes(tmp_path: Path) -> None:
    expect.path_absent(phase=1, path=tmp_path / "absent.txt")


def test_path_absent_fails(tmp_path: Path) -> None:
    f = tmp_path / "exists.txt"
    f.write_text("x")
    with pytest.raises(expect.PhaseAssertionError):
        expect.path_absent(phase=1, path=f)


def test_json_field_equals(tmp_path: Path) -> None:
    f = tmp_path / "doc.json"
    f.write_text(json.dumps({"schema_version": 7, "name": "x"}))
    expect.json_field(phase=1, path=f, key="schema_version", expected=7)


def test_json_field_mismatch_raises(tmp_path: Path) -> None:
    f = tmp_path / "doc.json"
    f.write_text(json.dumps({"schema_version": 6}))
    with pytest.raises(expect.PhaseAssertionError) as exc:
        expect.json_field(phase=1, path=f, key="schema_version", expected=7)
    assert "schema_version" in str(exc.value)
    assert "expected=7" in str(exc.value)


def test_json_field_in_allowed(tmp_path: Path) -> None:
    f = tmp_path / "doc.json"
    f.write_text(json.dumps({"match_quality": "ast"}))
    expect.json_field_in(phase=1, path=f, key="match_quality", allowed=["ast", "exact", "fallback", "none"])


def test_file_size_between(tmp_path: Path) -> None:
    f = tmp_path / "size.bin"
    f.write_bytes(b"x" * 1024)
    expect.file_size_between(phase=1, path=f, min_bytes=1000, max_bytes=2000)
    with pytest.raises(expect.PhaseAssertionError):
        expect.file_size_between(phase=2, path=f, min_bytes=2000, max_bytes=3000)


def test_file_mode(tmp_path: Path) -> None:
    f = tmp_path / "mode.txt"
    f.write_text("x")
    f.chmod(0o600)
    expect.file_mode(phase=1, path=f, mode=0o600)
    with pytest.raises(expect.PhaseAssertionError):
        expect.file_mode(phase=2, path=f, mode=0o644)
```

- [ ] **Step 2: Run test (verify it fails)**

Run: `cd /Users/crisn/Documents/Projects/chameleon && PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/journey/harness/tests/test_expect.py -v`
Expected: FAIL with module not found.

- [ ] **Step 3: Write expect.py**

Create `tests/journey/harness/expect.py`:

```python
"""Assertion helpers. Each takes phase: int for failure attribution.

Raises PhaseAssertionError on miss. The runner catches this and records
the phase as FAIL, then continues with the next phase.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any


class PhaseAssertionError(Exception):
    def __init__(self, phase: int, message: str):
        self.phase = phase
        super().__init__(f"[phase {phase}] {message}")


def path_exists(phase: int, path: Path) -> None:
    if not path.exists():
        raise PhaseAssertionError(phase, f"expected path to exist: {path}")


def path_absent(phase: int, path: Path) -> None:
    if path.exists():
        raise PhaseAssertionError(phase, f"expected path to be absent: {path}")


def json_field(phase: int, path: Path, key: str, expected: Any) -> None:
    if not path.exists():
        raise PhaseAssertionError(phase, f"json file missing: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    actual = data
    for part in key.split("."):
        if isinstance(actual, dict) and part in actual:
            actual = actual[part]
        else:
            raise PhaseAssertionError(phase, f"key {key!r} not found in {path}")
    if actual != expected:
        raise PhaseAssertionError(
            phase, f"{path}: key={key} expected={expected!r}, got={actual!r}"
        )


def json_field_in(phase: int, path: Path, key: str, allowed: list[Any]) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    actual = data
    for part in key.split("."):
        actual = actual[part]
    if actual not in allowed:
        raise PhaseAssertionError(
            phase, f"{path}: key={key} value={actual!r} not in {allowed!r}"
        )


def file_size_between(phase: int, path: Path, min_bytes: int, max_bytes: int) -> None:
    if not path.exists():
        raise PhaseAssertionError(phase, f"file missing for size check: {path}")
    size = path.stat().st_size
    if not (min_bytes <= size <= max_bytes):
        raise PhaseAssertionError(
            phase, f"{path}: size={size} not in [{min_bytes}, {max_bytes}]"
        )


def file_mode(phase: int, path: Path, mode: int) -> None:
    if not path.exists():
        raise PhaseAssertionError(phase, f"file missing for mode check: {path}")
    actual_mode = stat.S_IMODE(path.stat().st_mode)
    if actual_mode != mode:
        raise PhaseAssertionError(
            phase, f"{path}: mode={oct(actual_mode)} expected={oct(mode)}"
        )


def env_var_set(phase: int, name: str, under: Path) -> None:
    """Verify an env var is set and its value points under `under`."""
    value = os.environ.get(name)
    if value is None:
        raise PhaseAssertionError(phase, f"env var {name} not set")
    if not Path(value).resolve().is_relative_to(under.resolve()):
        raise PhaseAssertionError(
            phase, f"env var {name}={value!r} is not under {under}"
        )


def no_chameleon_state_in_home(phase: int) -> None:
    """Isolation guard: developer's home dir must not be touched by the harness."""
    home_data = Path.home() / ".local" / "share" / "chameleon"
    home_hmac = Path.home() / ".claude" / "hooks" / ".exec_hmac.key"
    if home_data.exists():
        # Only fail if it was modified DURING the run; pre-existing is fine
        # so this check is just informational. Caller should snapshot mtime
        # before and compare.
        pass  # See snapshots.py for active isolation tracking.
    return None
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/crisn/Documents/Projects/chameleon && PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/journey/harness/tests/test_expect.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/journey/harness/expect.py tests/journey/harness/tests/test_expect.py
git commit -m "Add expect.* assertion helpers with phase attribution"
```

### Task 1.6: harness/snapshots.py (capture state per phase)

**Files:**
- Create: `tests/journey/harness/snapshots.py`

- [ ] **Step 1: Write snapshots.py (no unit test; this is glue)**

Create `tests/journey/harness/snapshots.py`:

```python
"""Capture chameleon state per phase for post-mortem inspection.

A snapshot is a recursive copy of:
  - <fixture>/.chameleon/  (the committed profile state)
  - <plugin_data_dir>/     (the per-run global state including drift.db)

into <run_dir>/snapshots/<act_id>/<phase_id>/.
"""
from __future__ import annotations

import shutil
from pathlib import Path


def capture(snapshot_root: Path, act_id: str, phase_id: int, sources: list[Path]) -> Path:
    """Copy each source path into snapshot_root/<act_id>/phase_<phase_id>/<name>/.

    Missing sources are skipped silently. Returns the destination directory.
    """
    dest = snapshot_root / act_id / f"phase_{phase_id:02d}"
    dest.mkdir(parents=True, exist_ok=True)

    for src in sources:
        if not src.exists():
            continue
        target = dest / src.name
        if src.is_dir():
            shutil.copytree(src, target, dirs_exist_ok=True, symlinks=True)
        else:
            shutil.copy2(src, target)
    return dest
```

- [ ] **Step 2: Commit**

```bash
git add tests/journey/harness/snapshots.py
git commit -m "Add snapshot capture helper"
```

### Task 1.7: harness/mcp.py (direct MCP stdio call)

**Files:**
- Create: `tests/journey/harness/mcp.py`

- [ ] **Step 1: Write mcp.py**

Create `tests/journey/harness/mcp.py`:

```python
"""Direct MCP tool calls via stdio. RUNNER INSTRUMENTATION ONLY.

Use for state introspection (e.g., list_profiles to verify registration),
NOT for replacing user-facing flows. Bypassing /chameleon-init with
bootstrap_repo() defeats the test purpose.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


def call_mcp_tool(
    tool_name: str,
    plugin_root: Path,
    env: dict[str, str],
    timeout_s: int = 30,
    **args: Any,
) -> dict:
    """Spawn the MCP server, call one tool, return its envelope.

    Each call is a fresh subprocess. For batched calls use a session
    (deferred to v2).
    """
    server_cmd = [
        str(plugin_root / "mcp" / ".venv" / "bin" / "python"),
        "-m",
        "chameleon_mcp.server",
    ]
    proc_env = {**env, "PYTHONPATH": str(plugin_root / "mcp")}

    init_msg = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "journey-harness", "version": "1.0"},
        },
    })
    initialized_notification = json.dumps({
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    })
    call_msg = json.dumps({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": args},
    })
    stdin_payload = init_msg + "\n" + initialized_notification + "\n" + call_msg + "\n"

    proc = subprocess.run(
        server_cmd,
        input=stdin_payload,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=proc_env,
        check=False,
    )

    # Parse last JSON-RPC response (id=2)
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("id") == 2:
            if "result" in obj:
                return obj["result"]
            if "error" in obj:
                raise RuntimeError(f"MCP error: {obj['error']}")
    raise RuntimeError(
        f"no response for tool {tool_name!r}; stdout={proc.stdout!r}, stderr={proc.stderr!r}"
    )
```

- [ ] **Step 2: Commit**

```bash
git add tests/journey/harness/mcp.py
git commit -m "Add direct MCP stdio call helper"
```

### Task 1.8: harness/claude.py (spawn claude -p + parse stream-json)

**Files:**
- Create: `tests/journey/harness/claude.py`
- Test: `tests/journey/harness/tests/test_claude_parser.py`

- [ ] **Step 1: Write failing test**

Create `tests/journey/harness/tests/test_claude_parser.py`:

```python
"""Unit tests for stream-json parsing (no actual claude spawn)."""
from __future__ import annotations

from tests.journey.harness.claude import parse_stream_json


SAMPLE_STREAM = """
{"type": "system", "subtype": "init", "session_id": "abc"}
{"type": "system", "subtype": "hook_response", "hook_name": "PreToolUse:Edit", "stdout": "{\\"hookSpecificOutput\\":{\\"additionalContext\\":\\"<chameleon-context>archetype=util</chameleon-context>\\"}}"}
{"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}
{"type": "result", "subtype": "success", "total_cost_usd": 0.12, "duration_ms": 4200}
""".strip()


def test_parse_cost() -> None:
    parsed = parse_stream_json(SAMPLE_STREAM)
    assert parsed.cost_usd == 0.12


def test_parse_hook_events() -> None:
    parsed = parse_stream_json(SAMPLE_STREAM)
    pre_tool_events = [e for e in parsed.hook_events if e.hook_name == "PreToolUse:Edit"]
    assert len(pre_tool_events) == 1
    assert "<chameleon-context>" in pre_tool_events[0].stdout


def test_parse_malformed_lines_skipped() -> None:
    """Malformed JSON lines are skipped, not raised."""
    stream = '{"type": "system", "subtype": "init"}\nthis is junk\n{"type": "result", "total_cost_usd": 0.05}'
    parsed = parse_stream_json(stream)
    assert parsed.cost_usd == 0.05
```

- [ ] **Step 2: Run test (verify it fails)**

Run: `cd /Users/crisn/Documents/Projects/chameleon && PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/journey/harness/tests/test_claude_parser.py -v`
Expected: FAIL with module not found.

- [ ] **Step 3: Write claude.py**

Create `tests/journey/harness/claude.py`:

```python
"""Spawn `claude -p` subprocess and parse stream-json output.

The parser is split from spawn_claude() so we can unit-test it without
spawning real Claude.
"""
from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path


@dataclasses.dataclass
class HookEvent:
    hook_name: str
    stdout: str


@dataclasses.dataclass
class ParsedSession:
    cost_usd: float
    hook_events: list[HookEvent]
    raw_lines: list[str]


def parse_stream_json(stream: str) -> ParsedSession:
    """Parse a stream-json transcript. Malformed lines are skipped."""
    cost = 0.0
    hook_events: list[HookEvent] = []
    raw_lines: list[str] = []

    for line in stream.splitlines():
        line = line.strip()
        if not line:
            continue
        raw_lines.append(line)
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        if obj.get("type") == "result":
            cost = float(obj.get("total_cost_usd", 0.0))
        elif obj.get("type") == "system" and obj.get("subtype") == "hook_response":
            hook_events.append(
                HookEvent(
                    hook_name=obj.get("hook_name", ""),
                    stdout=obj.get("stdout", ""),
                )
            )

    return ParsedSession(cost_usd=cost, hook_events=hook_events, raw_lines=raw_lines)


@dataclasses.dataclass
class ClaudeSession:
    cost_usd: float
    hook_events: list[HookEvent]
    transcript_path: Path
    returncode: int


def spawn_claude(
    prompt: str,
    cwd: Path,
    env: dict[str, str],
    transcript_path: Path,
    max_turns: int = 25,
    allowed_tools: list[str] | None = None,
    permission_mode: str = "acceptEdits",
    timeout_s: int = 900,
    model: str = "sonnet",
    plugin_root: Path | None = None,
) -> ClaudeSession:
    """Spawn `claude -p` and capture its stream-json output."""
    args = [
        "claude", "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--include-hook-events",
        "--max-turns", str(max_turns),
        "--model", model,
        "--permission-mode", permission_mode,
    ]
    if plugin_root is not None:
        args += ["--plugin-dir", str(plugin_root)]
    if allowed_tools:
        args += ["--allowedTools", ",".join(allowed_tools)]

    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # Persist whatever we have
        transcript_path.write_text(exc.stdout or "", encoding="utf-8")
        return ClaudeSession(
            cost_usd=0.0,
            hook_events=[],
            transcript_path=transcript_path,
            returncode=-1,
        )

    transcript_path.write_text(proc.stdout, encoding="utf-8")
    parsed = parse_stream_json(proc.stdout)
    return ClaudeSession(
        cost_usd=parsed.cost_usd,
        hook_events=parsed.hook_events,
        transcript_path=transcript_path,
        returncode=proc.returncode,
    )
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/crisn/Documents/Projects/chameleon && PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/journey/harness/tests/test_claude_parser.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/journey/harness/claude.py tests/journey/harness/tests/test_claude_parser.py
git commit -m "Add claude -p spawn + stream-json parser"
```

### Task 1.9: harness/git_shim.py (PATH shim with context-manager)

**Files:**
- Create: `tests/journey/harness/git_shim.py`

- [ ] **Step 1: Write git_shim.py**

Create `tests/journey/harness/git_shim.py`:

```python
"""Plant a fake `git` executable on PATH that sleeps before delegating.

Used to test trust.auto_preserve_when 2-second timeout (Phase 14).
ShimHandle supports context-manager protocol so PATH is restored even
if the test raises.
"""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


class ShimHandle:
    def __init__(self, shim_dir: Path, original_path: str):
        self.shim_dir = shim_dir
        self.original_path = original_path

    def __enter__(self) -> "ShimHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.restore()

    def restore(self) -> None:
        """Restore PATH to its pre-shim value. Idempotent."""
        if os.environ.get("PATH") != self.original_path:
            os.environ["PATH"] = self.original_path
        # shim_dir cleanup deferred to caller (often the run_dir cleanup)


def setup_git_shim(delay_seconds: float, shim_dir_parent: Path) -> ShimHandle:
    """Plant a fake `git` that sleeps, then exec real git. Returns ShimHandle.

    Usage:
        with setup_git_shim(5.0, ctx.run_dir / "shim") as shim:
            # any git invocation now sleeps 5s before real execution
            ...
    """
    shim_dir = shim_dir_parent / "git_shim"
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim_path = shim_dir / "git"

    # Find real git
    original_path = os.environ.get("PATH", "")
    real_git = None
    for d in original_path.split(":"):
        candidate = Path(d) / "git"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            real_git = str(candidate)
            break
    if real_git is None:
        raise RuntimeError("could not locate real git binary on PATH")

    shim_path.write_text(
        f"#!/bin/bash\nsleep {delay_seconds}\nexec {real_git} \"$@\"\n",
        encoding="utf-8",
    )
    shim_path.chmod(shim_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    os.environ["PATH"] = f"{shim_dir}:{original_path}"
    return ShimHandle(shim_dir=shim_dir, original_path=original_path)
```

- [ ] **Step 2: Commit**

```bash
git add tests/journey/harness/git_shim.py
git commit -m "Add git shim helper with context-manager cleanup"
```

### Task 1.10: harness/preflight.py (runner preflight checks)

**Files:**
- Create: `tests/journey/harness/preflight.py`

- [ ] **Step 1: Write preflight.py**

Create `tests/journey/harness/preflight.py`:

```python
"""Runner-side preflight checks. Abort before any Claude spawn if missing.

Checked:
  - claude CLI on PATH
  - git --version >= 2.28
  - committed seed fixtures present
  - mcp/.venv/bin/python present
  - no concurrent runner (lockfile in run_dir parent)
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from tests.journey.harness.bash import run_bash
from tests.journey.harness.fixtures import check_git_version


class PreflightError(Exception):
    pass


def claude_on_path() -> Path:
    p = shutil.which("claude")
    if not p:
        raise PreflightError("`claude` CLI not on PATH; install Claude Code or unset CHAMELEON_TEST_NO_CLAUDE")
    return Path(p)


def python_venv_present(plugin_root: Path) -> Path:
    p = plugin_root / "mcp" / ".venv" / "bin" / "python"
    if not p.is_file():
        raise PreflightError(
            f"missing {p}; run `cd mcp && uv sync` from the chameleon repo first"
        )
    return p


def fixtures_present(plugin_root: Path) -> dict[str, Path]:
    fixtures_root = plugin_root / "tests" / "journey" / "fixtures"
    required = ["ts_basic", "rails_basic", "ts_monorepo", "ts_with_rails_sidecar"]
    found: dict[str, Path] = {}
    missing: list[str] = []
    for name in required:
        path = fixtures_root / name
        if not path.is_dir() or not any(path.iterdir()):
            missing.append(name)
        else:
            found[name] = path
    if missing:
        raise PreflightError(
            f"missing fixtures: {missing}; expected under {fixtures_root}"
        )
    return found


def acquire_lock(run_dir: Path) -> Path:
    """Acquire an exclusive lock for the current run_dir. Returns path."""
    lock_path = run_dir / ".lock"
    # run_dir is already unique per timestamp; if .lock exists, another runner
    # is using this exact path (extremely unlikely race). Abort with diagnostic.
    if lock_path.exists():
        raise PreflightError(f"another runner has acquired {lock_path}; aborting")
    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    return lock_path


def run_all(plugin_root: Path, run_dir: Path) -> dict:
    """Run every preflight check. Returns a dict of resolved paths."""
    return {
        "claude": claude_on_path(),
        "git_version": check_git_version((2, 28)),
        "python_venv": python_venv_present(plugin_root),
        "fixtures": fixtures_present(plugin_root),
        "lock_path": acquire_lock(run_dir),
    }
```

- [ ] **Step 2: Commit**

```bash
git add tests/journey/harness/preflight.py
git commit -m "Add preflight checks for runner"
```

---

## Phase 2: Fixtures

Fixtures are committed seed files (no `.git/`). Each fixture exercises specific chameleon code paths. Keep them MINIMAL but realistic.

### Task 2.1: fixtures/ts_basic/

**Files:**
- Create: `tests/journey/fixtures/ts_basic/package.json`
- Create: `tests/journey/fixtures/ts_basic/tsconfig.json`
- Create: `tests/journey/fixtures/ts_basic/.eslintrc.cjs`
- Create: `tests/journey/fixtures/ts_basic/src/components/Button.tsx` (and ~9 other components)
- Create: `tests/journey/fixtures/ts_basic/src/hooks/useFetch.ts` (and ~4 other hooks)
- Create: `tests/journey/fixtures/ts_basic/src/utils/format_date.ts` (and ~4 other utils)
- Create: `tests/journey/fixtures/ts_basic/src/types/api.ts`
- Create: `tests/journey/fixtures/ts_basic/tests/Button.test.tsx`

- [ ] **Step 1: Create package.json + tsconfig + eslintrc**

Create `tests/journey/fixtures/ts_basic/package.json`:

```json
{
  "name": "ts-basic-fixture",
  "version": "0.0.0",
  "private": true,
  "type": "module",
  "scripts": { "lint": "eslint src/**/*.{ts,tsx}", "test": "vitest" },
  "devDependencies": {
    "@types/react": "^18.2.0",
    "eslint": "^9.0.0",
    "typescript": "^5.4.0",
    "vitest": "^1.0.0",
    "react": "^18.2.0"
  }
}
```

Create `tests/journey/fixtures/ts_basic/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noImplicitAny": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "paths": { "@/*": ["./src/*"] }
  },
  "include": ["src", "tests"]
}
```

Create `tests/journey/fixtures/ts_basic/.eslintrc.cjs`:

```js
module.exports = {
  root: true,
  parser: "@typescript-eslint/parser",
  rules: { "no-unused-vars": "warn", "no-console": "warn" }
};
```

- [ ] **Step 2: Create ~10 component files (similar pattern)**

Create `tests/journey/fixtures/ts_basic/src/components/Button.tsx`:

```tsx
import { type ReactNode } from "react";

type ButtonProps = {
  children: ReactNode;
  onClick: () => void;
  variant?: "primary" | "secondary";
};

export function Button({ children, onClick, variant = "primary" }: ButtonProps) {
  return (
    <button className={`btn btn-${variant}`} onClick={onClick}>
      {children}
    </button>
  );
}
```

Create 9 more files with the same shape under `src/components/`: `Card.tsx`, `Modal.tsx`, `Tooltip.tsx`, `Avatar.tsx`, `Badge.tsx`, `Dropdown.tsx`, `Input.tsx`, `Spinner.tsx`, `Tabs.tsx`. Each should be a function component with typed props + a `.tsx` extension + named export.

- [ ] **Step 3: Create ~5 hook files**

Create `tests/journey/fixtures/ts_basic/src/hooks/useFetch.ts`:

```ts
import { useState, useEffect } from "react";

export function useFetch<T>(url: string): { data: T | null; loading: boolean; error: Error | null } {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    fetch(url)
      .then(r => r.json() as Promise<T>)
      .then(setData)
      .catch(setError)
      .finally(() => setLoading(false));
  }, [url]);

  return { data, loading, error };
}
```

Create 4 more hooks: `useDebounce.ts`, `useLocalStorage.ts`, `useToggle.ts`, `usePrevious.ts`. Each is a named export starting with `use`.

- [ ] **Step 4: Create ~5 util files**

Create `tests/journey/fixtures/ts_basic/src/utils/format_date.ts`:

```ts
export function formatDate(date: Date): string {
  return date.toISOString().slice(0, 10);
}

export function parseDate(s: string): Date {
  return new Date(s);
}
```

Create 4 more utils: `format_currency.ts`, `slugify.ts`, `clamp.ts`, `debounce.ts`. Each exports 1-3 named functions.

- [ ] **Step 5: Create types + test files**

Create `tests/journey/fixtures/ts_basic/src/types/api.ts`:

```ts
export type ApiResponse<T> = {
  data: T;
  meta: { total: number; page: number };
};
```

Create `tests/journey/fixtures/ts_basic/tests/Button.test.tsx`:

```tsx
import { describe, it, expect } from "vitest";
import { Button } from "../src/components/Button";

describe("Button", () => {
  it("renders children", () => {
    const node = Button({ children: "Hi", onClick: () => {} });
    expect(node).toBeDefined();
  });
});
```

- [ ] **Step 6: Verify file count**

Run: `find tests/journey/fixtures/ts_basic -type f | wc -l`
Expected: ~25-30 files.

- [ ] **Step 7: Commit**

```bash
git add tests/journey/fixtures/ts_basic/
git commit -m "Add ts_basic seed fixture (~25 files)"
```

### Task 2.2: fixtures/rails_basic/

**Files:** ~30 Rails files (Gemfile, app/controllers, app/models, app/services, spec/, .rubocop.yml).

- [ ] **Step 1: Create Gemfile + .rubocop.yml + config skeleton**

Create `tests/journey/fixtures/rails_basic/Gemfile`:

```ruby
source "https://rubygems.org"
ruby "3.3.0"

gem "rails", "~> 7.1"
gem "pg"
gem "puma"
gem "sidekiq"

group :development, :test do
  gem "rspec-rails"
  gem "rubocop", require: false
  gem "factory_bot_rails"
end
```

Create `tests/journey/fixtures/rails_basic/.rubocop.yml`:

```yaml
AllCops:
  TargetRubyVersion: 3.3
  NewCops: enable

Style/StringLiterals:
  EnforcedStyle: double_quotes

Layout/LineLength:
  Max: 120

Metrics/ClassLength:
  Max: 150

Metrics/MethodLength:
  Max: 30
```

Create `tests/journey/fixtures/rails_basic/config/application.rb`:

```ruby
require "rails/all"

module RailsBasic
  class Application < Rails::Application
    config.load_defaults 7.1
  end
end
```

- [ ] **Step 2: Create ~8 controllers**

Create `tests/journey/fixtures/rails_basic/app/controllers/application_controller.rb`:

```ruby
class ApplicationController < ActionController::Base
  protect_from_forgery with: :exception

  private

  def current_user
    @current_user ||= User.find_by(id: session[:user_id])
  end
end
```

Create 7 more controllers under `app/controllers/`: `users_controller.rb`, `posts_controller.rb`, `comments_controller.rb`, `sessions_controller.rb`, `accounts_controller.rb`, `tags_controller.rb`, `search_controller.rb`. Each follows the Rails RESTful pattern (`def index; def show; def create; def update; def destroy`).

- [ ] **Step 3: Create ~7 models**

Create `tests/journey/fixtures/rails_basic/app/models/user.rb`:

```ruby
class User < ApplicationRecord
  has_many :posts
  has_many :comments

  validates :email, presence: true, uniqueness: true
  validates :name, presence: true
end
```

Create 6 more: `post.rb`, `comment.rb`, `tag.rb`, `taggable.rb`, `account.rb`, `application_record.rb`. Each `< ApplicationRecord` with associations and validations.

- [ ] **Step 4: Create ~5 services**

Create `tests/journey/fixtures/rails_basic/app/services/users/create_user.rb`:

```ruby
module Users
  class CreateUser
    def initialize(params)
      @params = params
    end

    def call
      User.create!(@params)
    rescue ActiveRecord::RecordInvalid => e
      Result.failure(e.message)
    end
  end
end
```

Create 4 more: `users/update_user.rb`, `posts/publish_post.rb`, `comments/moderate_comment.rb`, `accounts/billing_sync.rb`. Each is a domain service module + class with `#call`.

- [ ] **Step 5: Create ~8 spec files**

Create `tests/journey/fixtures/rails_basic/spec/models/user_spec.rb`:

```ruby
require "rails_helper"

RSpec.describe User, type: :model do
  it "validates presence of email" do
    user = User.new(email: nil, name: "x")
    expect(user).not_to be_valid
  end
end
```

Create 7 more under `spec/`: `models/post_spec.rb`, `models/comment_spec.rb`, `controllers/users_controller_spec.rb`, `controllers/posts_controller_spec.rb`, `services/users/create_user_spec.rb`, `services/posts/publish_post_spec.rb`, `rails_helper.rb`.

- [ ] **Step 6: Verify**

Run: `find tests/journey/fixtures/rails_basic -type f | wc -l`
Expected: ~30 files.

- [ ] **Step 7: Commit**

```bash
git add tests/journey/fixtures/rails_basic/
git commit -m "Add rails_basic seed fixture (~30 files)"
```

### Task 2.3: fixtures/ts_monorepo/

**Files:** Root package.json (workspaces) + `packages/api/` + `packages/web/`, each with own package.json + a few source files.

- [ ] **Step 1: Create root + workspace skeletons**

Create `tests/journey/fixtures/ts_monorepo/package.json`:

```json
{
  "name": "ts-monorepo-fixture",
  "private": true,
  "workspaces": ["packages/*"]
}
```

Create `tests/journey/fixtures/ts_monorepo/packages/api/package.json`:

```json
{
  "name": "@monorepo/api",
  "version": "0.0.0",
  "main": "src/index.ts"
}
```

Create `tests/journey/fixtures/ts_monorepo/packages/api/src/index.ts`:

```ts
export function startServer(port: number): void {
  console.log(`api on ${port}`);
}
```

Create 4 more files under `packages/api/src/`: `routes.ts`, `db.ts`, `auth.ts`, `health.ts`.

Create `tests/journey/fixtures/ts_monorepo/packages/web/package.json`:

```json
{
  "name": "@monorepo/web",
  "version": "0.0.0",
  "main": "src/index.tsx"
}
```

Create 5 files under `packages/web/src/`: `index.tsx`, `App.tsx`, `routes.tsx`, `client.ts`, `theme.ts`.

- [ ] **Step 2: Verify**

Run: `find tests/journey/fixtures/ts_monorepo -type f`
Expected: 13 files (root package.json + 2 workspace package.json + ~10 source files).

- [ ] **Step 3: Commit**

```bash
git add tests/journey/fixtures/ts_monorepo/
git commit -m "Add ts_monorepo seed fixture with 2 workspaces"
```

### Task 2.4: fixtures/ts_with_rails_sidecar/

**Files:** Rails skeleton + `client/` TS subdir. Used to verify `language_hint` hybrid detection.

- [ ] **Step 1: Create Rails side + client side**

Create `tests/journey/fixtures/ts_with_rails_sidecar/Gemfile`:

```ruby
source "https://rubygems.org"
gem "rails", "~> 7.1"
gem "pg"
```

Create `tests/journey/fixtures/ts_with_rails_sidecar/app/controllers/api/v1/widgets_controller.rb`:

```ruby
module Api
  module V1
    class WidgetsController < ApplicationController
      def index
        render json: Widget.all
      end
    end
  end
end
```

Create `tests/journey/fixtures/ts_with_rails_sidecar/app/models/widget.rb`:

```ruby
class Widget < ApplicationRecord
  validates :name, presence: true
end
```

Create `tests/journey/fixtures/ts_with_rails_sidecar/client/package.json`:

```json
{
  "name": "client",
  "version": "0.0.0",
  "main": "src/index.tsx"
}
```

Create `tests/journey/fixtures/ts_with_rails_sidecar/client/src/index.tsx`:

```tsx
import { Widget } from "./Widget";
export { Widget };
```

Create `tests/journey/fixtures/ts_with_rails_sidecar/client/src/Widget.tsx`:

```tsx
export function Widget({ name }: { name: string }) {
  return <div className="widget">{name}</div>;
}
```

- [ ] **Step 2: Verify**

Run: `find tests/journey/fixtures/ts_with_rails_sidecar -type f`
Expected: ~6 files spanning Ruby + TS.

- [ ] **Step 3: Commit**

```bash
git add tests/journey/fixtures/ts_with_rails_sidecar/
git commit -m "Add ts_with_rails_sidecar fixture for language_hint hybrid test"
```

---

## Phase 3: Runner

### Task 3.1: runner.py with argparse + preflight

**Files:**
- Create: `tests/journey/runner.py`

- [ ] **Step 1: Write runner.py**

Create `tests/journey/runner.py`:

```python
"""Journey harness runner.

Usage:
  mcp/.venv/bin/python -m tests.journey.runner               # full run
  mcp/.venv/bin/python -m tests.journey.runner --list        # list acts
  mcp/.venv/bin/python -m tests.journey.runner --dry-run     # preflight only
  mcp/.venv/bin/python -m tests.journey.runner --max-budget-usd 30
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Ensure repo root on sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.journey.harness.context import JourneyContext, build_context
from tests.journey.harness.fixtures import setup_fixture
from tests.journey.harness import preflight


_ACTS = [
    ("00_preflight", "Pre-flight wipe + isolation setup", 0.30, [0]),
    ("01_install_mcp_doctor", "Install + MCP boot + Doctor + using-chameleon verify", 1.20, [1, 2, 3, 4]),
    ("02_init_flow", "Init flow (TS, both auto_rename modes + force=True)", 3.00, [5, 6, 7, 15]),
    ("03_hot_path_drift", "Hot path advisory + drift (Edit + Write + NotebookEdit)", 3.00, [8, 9, 10, 11]),
    ("04_v060_ux_bundle", "v0.6.0 UX bundle", 3.50, [12, 13, 14]),
    ("05_teach_status_doctor", "Teach + Status + Doctor", 2.50, [16, 17, 18]),
    ("06_suppression_callout", "Suppression + callout-detector", 2.00, [19, 20, 23]),
    ("07_rails_parity", "Rails parity", 3.00, [21]),
    ("08_hooks_security_sanitization", "Hooks + security + sanitization", 2.00, [22, 24, 25, 26]),
    ("09_schema_atomicity_concurrency", "Schema + atomicity + concurrency + monorepo", 2.50, [27, 28, 29, 30, 31, 32]),
    ("10_daemon_observability_resilience", "Daemon + observability + resilience", 2.00, [33, 34, 35, 36]),
    ("11_uninstall_cleanup", "Uninstall + cleanup", 0.50, [37]),
]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tests.journey.runner")
    p.add_argument("--list", action="store_true", help="List acts + phases and exit")
    p.add_argument("--dry-run", action="store_true", help="Run preflight only, no Claude spawn")
    p.add_argument("--max-budget-usd", type=float, default=35.0, help="Abort if projected cost exceeds (default 35)")
    p.add_argument(
        "--results-dir",
        default=str(_REPO_ROOT / "tests" / "journey" / "results"),
        help="Where to write per-run output",
    )
    return p


def cmd_list() -> int:
    print(f"{len(_ACTS)} acts:", file=sys.stderr)
    for act_id, name, ceiling, phases in _ACTS:
        print(f"  {act_id:40s}  ${ceiling:>5.2f}  phases={phases}  {name}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list:
        return cmd_list()

    # Pre-flight check: estimated cost vs budget
    total_estimated = sum(a[2] for a in _ACTS)
    if total_estimated > args.max_budget_usd:
        print(
            f"ERROR: estimated total cost ${total_estimated:.2f} > --max-budget-usd ${args.max_budget_usd:.2f}",
            file=sys.stderr,
        )
        return 1

    # Build context (creates run_dir + subdirs + sets env keys, doesn't apply them yet)
    results_root = Path(args.results_dir).resolve()
    results_root.mkdir(parents=True, exist_ok=True)
    ctx = build_context(plugin_root=_REPO_ROOT, results_root=results_root)
    print(f"run_dir: {ctx.run_dir}", file=sys.stderr)

    # Preflight: claude, git, fixtures, python, lockfile
    try:
        pf = preflight.run_all(plugin_root=_REPO_ROOT, run_dir=ctx.run_dir)
    except preflight.PreflightError as e:
        print(f"PREFLIGHT FAILED: {e}", file=sys.stderr)
        return 2

    print(f"preflight ok: claude={pf['claude']}, git={pf['git_version']}", file=sys.stderr)

    # Copy fixtures to <run_dir>/working/
    for name, seed_path in pf["fixtures"].items():
        try:
            work_dir, origin_dir = setup_fixture(name, seed_path, ctx.run_dir / "working")
        except Exception as e:
            print(f"fixture {name} setup failed: {e}", file=sys.stderr)
            return 3
        ctx.fixtures[name] = work_dir
        ctx.origins[name] = origin_dir
    print(f"fixtures ready: {list(ctx.fixtures)}", file=sys.stderr)

    if args.dry_run:
        print("DRY RUN complete (no acts executed)", file=sys.stderr)
        return 0

    # Run acts in order with mid-run abort
    return _run_acts(ctx, args)


def _run_acts(ctx: JourneyContext, args: argparse.Namespace) -> int:
    """Sequentially run each act, applying mid-run abort budget check."""
    all_results: list[dict] = []
    any_failed = False

    for idx, (act_id, name, ceiling, phases) in enumerate(_ACTS):
        # Mid-run abort: cost_so_far + remaining_act_ceilings > budget?
        remaining_ceilings = [a[2] for a in _ACTS[idx:]]
        projected = ctx.cost_so_far_usd + sum(remaining_ceilings)
        if projected > args.max_budget_usd:
            print(
                f"BUDGET ABORT before {act_id}: projected ${projected:.2f} > ${args.max_budget_usd:.2f}",
                file=sys.stderr,
            )
            for skipped_idx in range(idx, len(_ACTS)):
                skip_act_id = _ACTS[skipped_idx][0]
                skip_phases = _ACTS[skipped_idx][3]
                for ph in skip_phases:
                    all_results.append({
                        "act": skip_act_id,
                        "phase": ph,
                        "status": "SKIP",
                        "notes": "budget exhausted",
                    })
            break

        # Set up the per-act checkpoint file
        ctx.current_checkpoint_file = ctx.run_dir / "checkpoints" / f"{act_id}.jsonl"
        ctx.current_checkpoint_file.touch()
        ctx.env["CHAMELEON_JOURNEY_CHECKPOINT"] = str(ctx.current_checkpoint_file)

        print(f"[ACT {act_id}] {name} - starting (estimate ${ceiling:.2f})", file=sys.stderr)

        # Dynamic import + run
        mod = importlib.import_module(f"tests.journey.acts.act_{act_id}")
        t0 = time.monotonic()
        try:
            act_result = mod.run(ctx)
        except Exception as exc:
            elapsed = time.monotonic() - t0
            print(f"[ACT {act_id}] ERROR ({elapsed:.1f}s): {exc}", file=sys.stderr)
            for ph in phases:
                all_results.append({"act": act_id, "phase": ph, "status": "ERROR", "notes": str(exc)})
            any_failed = True
            continue

        elapsed = time.monotonic() - t0
        ctx.cost_so_far_usd += act_result.cost_usd

        print(
            f"[ACT {act_id}] done in {elapsed:.1f}s, cost ${act_result.cost_usd:.2f} (cumulative ${ctx.cost_so_far_usd:.2f})",
            file=sys.stderr,
        )
        for phase_outcome in act_result.phase_outcomes:
            all_results.append({
                "act": act_id,
                "phase": phase_outcome.phase,
                "status": phase_outcome.status,
                "notes": phase_outcome.notes,
            })
            if phase_outcome.status in ("FAIL", "ERROR"):
                any_failed = True

    _write_outputs(ctx, all_results)
    return 1 if any_failed else 0


def _write_outputs(ctx: JourneyContext, results: list[dict]) -> None:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = ctx.run_dir / "run.json"
    json_path.write_text(json.dumps({
        "timestamp": ts,
        "cost_so_far_usd": ctx.cost_so_far_usd,
        "results": results,
    }, indent=2), encoding="utf-8")

    lines = ["# Journey run", "", f"Run at {ts}", "", f"Total cost: ${ctx.cost_so_far_usd:.2f}", "",
             "| act | phase | status | notes |", "|-----|-------|--------|-------|"]
    for r in results:
        lines.append(f"| {r['act']} | {r['phase']} | {r['status']} | {r['notes'][:80]} |")

    (ctx.run_dir / "run.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"results: {json_path}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Test --list flag**

Run: `cd /Users/crisn/Documents/Projects/chameleon && PYTHONPATH=. mcp/.venv/bin/python -m tests.journey.runner --list`
Expected: 12 acts listed.

- [ ] **Step 3: Commit**

```bash
git add tests/journey/runner.py
git commit -m "Add runner.py with argparse + preflight + act orchestration"
```

### Task 3.2: acts/act_base.py (ActResult dataclass)

**Files:**
- Create: `tests/journey/acts/act_base.py`

- [ ] **Step 1: Write act_base.py**

Create `tests/journey/acts/act_base.py`:

```python
"""Common types + helpers used by all act modules."""
from __future__ import annotations

import dataclasses
from typing import Any

from tests.journey.harness.checkpoints import PhaseOutcome


@dataclasses.dataclass
class ActResult:
    act_id: str
    cost_usd: float
    phase_outcomes: list[PhaseOutcome]
    checkpoint_parse_errors: int = 0
    notes: str = ""


_CHECKPOINT_PREAMBLE = """\
At each phase boundary, emit a checkpoint by running this Bash command:

  echo '{"phase": <N>, "status": "started", "ts": "'$(date -u +%FT%TZ)'"}' >> "$CHAMELEON_JOURNEY_CHECKPOINT"

Then run the phase steps. When the phase succeeds, emit:

  echo '{"phase": <N>, "status": "completed", "ts": "'$(date -u +%FT%TZ)'"}' >> "$CHAMELEON_JOURNEY_CHECKPOINT"

If an assertion fails inside the phase, emit:

  echo '{"phase": <N>, "status": "failed", "ts": "'$(date -u +%FT%TZ)'", "notes": "what failed"}' >> "$CHAMELEON_JOURNEY_CHECKPOINT"

Emit each checkpoint as a SINGLE LINE outside any code fence. Never wrap them in markdown.
"""


def checkpoint_preamble() -> str:
    return _CHECKPOINT_PREAMBLE


def build_act_prompt(body: str) -> str:
    return checkpoint_preamble() + "\n\n" + body
```

- [ ] **Step 2: Commit**

```bash
git add tests/journey/acts/act_base.py
git commit -m "Add ActResult + checkpoint prompt preamble"
```

---

## Phase 4: Acts

Each act follows the same pattern: runner-side setup via `ctx.spawn_bash` and direct MCP calls, one `claude -p` session with multi-turn prompt, then runner-side assertions. The Claude prompts are derived from the spec's "12 acts" section verbatim.

I lay out the full pattern for Act 0 then summarize each subsequent act with the specific prompt content + assertions.

### Task 4.0: act_00_preflight.py

**Files:**
- Create: `tests/journey/acts/act_00_preflight.py`

- [ ] **Step 1: Write Act 0**

Create `tests/journey/acts/act_00_preflight.py`:

```python
"""Act 0: Pre-flight wipe + isolation setup.

This act is mostly runner-side scaffolding. The runner has already created
<run_dir>/* in build_context() and copied fixtures via setup_fixture() in
runner.py. This act verifies isolation and emits a single checkpoint.
"""
from __future__ import annotations

from pathlib import Path

from tests.journey.acts.act_base import ActResult
from tests.journey.harness import expect
from tests.journey.harness.checkpoints import PhaseOutcome
from tests.journey.harness.context import JourneyContext


def run(ctx: JourneyContext) -> ActResult:
    phase = 0
    notes: list[str] = []

    try:
        # Env vars point under run_dir
        for var in ("CHAMELEON_PLUGIN_DATA", "CHAMELEON_HMAC_KEY_PATH", "TMPDIR", "CHAMELEON_HOOK_ERROR_LOG"):
            value = ctx.env.get(var)
            assert value, f"{var} not set in ctx.env"
            assert str(ctx.run_dir) in value, f"{var}={value!r} is not under {ctx.run_dir}"

        # Per-run dirs exist + are empty (or only contain harness scaffolding)
        expect.path_exists(phase, ctx.plugin_data_dir)
        expect.path_exists(phase, ctx.tmpdir)
        expect.path_exists(phase, ctx.run_dir / "working")
        expect.path_exists(phase, ctx.run_dir / "checkpoints")

        # Home dir guard: developer's own chameleon data must NOT be inside run_dir
        home_data = Path.home() / ".local" / "share" / "chameleon"
        if home_data.exists():
            # If dev has chameleon data, ensure run_dir is NOT a parent of it (silly check, but enforces isolation intent)
            try:
                home_data.resolve().relative_to(ctx.run_dir.resolve())
                raise AssertionError("home dir is inside run_dir, isolation broken")
            except ValueError:
                pass  # expected: home_data is outside run_dir

        outcome = PhaseOutcome(phase=phase, status="PASS", notes="; ".join(notes) or "isolation verified")
    except (expect.PhaseAssertionError, AssertionError) as e:
        outcome = PhaseOutcome(phase=phase, status="FAIL", notes=str(e))

    return ActResult(
        act_id="00_preflight",
        cost_usd=0.0,
        phase_outcomes=[outcome],
        checkpoint_parse_errors=0,
    )
```

- [ ] **Step 2: Test Act 0 via runner**

Run: `cd /Users/crisn/Documents/Projects/chameleon && PYTHONPATH=. mcp/.venv/bin/python -m tests.journey.runner --dry-run`
Expected: Preflight runs through, dry-run exits 0 before invoking acts (Act 0 not executed in dry-run mode).

- [ ] **Step 3: Commit**

```bash
git add tests/journey/acts/act_00_preflight.py
git commit -m "Add act 0: pre-flight isolation verification"
```

### Task 4.1: act_01_install_mcp_doctor.py

**Files:** Create `tests/journey/acts/act_01_install_mcp_doctor.py`.

Spec ref: spec line ~207-213. This act drives a Claude session that verifies plugin install, boots MCP, lists 20 tools, runs `doctor`, inspects vendor checksums, verifies using-chameleon skill body in session context.

- [ ] **Step 1: Write Act 1**

Create the file with this structure (full prompt from spec embedded; concrete code follows the same shape as Act 0):

```python
"""Act 1: Install + MCP boot + Doctor + using-chameleon verify (Phases 1-4)."""
from __future__ import annotations

from pathlib import Path

from tests.journey.acts.act_base import ActResult, build_act_prompt
from tests.journey.harness import expect, mcp
from tests.journey.harness.checkpoints import parse_checkpoint_file
from tests.journey.harness.claude import spawn_claude
from tests.journey.harness.context import JourneyContext


_PROMPT_BODY = """\
Verify the chameleon plugin install.

PHASE 1, manifests:
  emit checkpoint started phase 1
  Use the Bash tool to `ls` and parse each of:
    .claude-plugin/plugin.json
    .claude-plugin/marketplace.json
    .cursor-plugin/plugin.json
    .codex-plugin/plugin.json
    gemini-extension.json
    hooks/hooks.json
  Verify each is valid JSON. Verify the chameleon plugin name is present.
  emit checkpoint completed phase 1

PHASE 2, MCP boot + 20 tools:
  emit checkpoint started phase 2
  The MCP server is launched automatically by Claude Code (chameleon-mcp).
  Use the chameleon-mcp::doctor tool (a no-arg tool). Verify the response.
  Also verify the tool registry: count the chameleon-mcp::* tools you have
  access to via your tool listing. Expected: 20 tools.
  emit checkpoint completed phase 2

PHASE 3, Doctor baseline:
  emit checkpoint started phase 3
  Inspect the doctor envelope. All 9 subsystems should report status "ok":
  python, bash, timeout, plugin_data_writable, hook_scripts, hmac_key,
  daemon, recent_errors, per_repo_state. Report any non-ok subsystem.
  emit checkpoint completed phase 3

PHASE 4, bootstrap resource limits + using-chameleon:
  emit checkpoint started phase 4
  Use Bash to verify `mcp/typescript-checksums.json` exists. Parse it,
  count entries. Verify each listed file exists under mcp/node_modules/typescript/.
  Then describe (in plain text) what you can see of the using-chameleon
  skill content in your current session context. The runner will inspect
  your transcript for chameleon-context markers in the SessionStart system message.
  emit checkpoint completed phase 4

Reminder: emit checkpoints as plain Bash echo lines, never inside code fences.
"""


def run(ctx: JourneyContext) -> ActResult:
    cwd = ctx.fixture("ts_basic")
    transcript = ctx.run_dir / "transcripts" / "act_01.txt"
    transcript.parent.mkdir(exist_ok=True)

    session = spawn_claude(
        prompt=build_act_prompt(_PROMPT_BODY),
        cwd=cwd,
        env={**ctx.env, "CHAMELEON_JOURNEY_CHECKPOINT": str(ctx.current_checkpoint_file)},
        transcript_path=transcript,
        max_turns=15,
        plugin_root=ctx.plugin_root,
        timeout_s=600,
    )

    outcomes, parse_errors = parse_checkpoint_file(
        ctx.current_checkpoint_file, expected_phases=[1, 2, 3, 4]
    )

    # Runner-side additional assertions
    notes_extra: dict[int, str] = {}
    try:
        # Phase 2 cross-check: query MCP directly for tools/list
        tools_result = mcp.call_mcp_tool(
            tool_name="doctor",  # use doctor as a roundtrip probe
            plugin_root=ctx.plugin_root,
            env=ctx.env,
        )
        if tools_result.get("status") != "ok":
            notes_extra[2] = f"MCP doctor returned status={tools_result.get('status')!r}"
    except Exception as e:
        notes_extra[2] = f"MCP direct probe failed: {e}"

    # Phase 4 cross-check: chameleon-context marker in transcript
    if "<chameleon-context>" not in transcript.read_text(encoding="utf-8"):
        notes_extra[4] = "no <chameleon-context> in transcript (using-chameleon not injected?)"

    for phase, extra in notes_extra.items():
        if phase in outcomes and outcomes[phase].status == "PASS":
            # Demote to FAIL if cross-check found an issue
            outcomes[phase].status = "FAIL"
            outcomes[phase].notes = (outcomes[phase].notes + "; " + extra).strip("; ")

    return ActResult(
        act_id="01_install_mcp_doctor",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
```

- [ ] **Step 2: Commit**

```bash
git add tests/journey/acts/act_01_install_mcp_doctor.py
git commit -m "Add act 1: install + MCP boot + doctor + using-chameleon verify"
```

### Tasks 4.2 through 4.11: Acts 2 through 11

Each follows the same skeleton as Act 1: `_PROMPT_BODY` (literal text from spec) + `def run(ctx)` that spawns Claude, parses checkpoints, runs runner-side cross-checks, returns ActResult.

For each act, the spec section ("Act N: <name>") in `docs/superpowers/specs/2026-05-21-journey-harness-design.md` is the source of truth for the Claude prompt body. The runner-side cross-checks per phase are listed in the spec's "38 phases" table (column "Specific assertions").

Per-act create files:

- [ ] **Task 4.2: Write `acts/act_02_init_flow.py`** (Phases 5, 6, 7, 15). Two TS fixtures bootstrapped (auto_rename=false and auto_rename=true); test `bootstrap_repo(force=True)` overwrite path; verify `archetype_renames.json` FIFO 256-cap. Prompt from spec line 217-230. Runner-side check: read `working/ts_basic/.chameleon/profile.json` for `schema_version`; verify `archetype_renames.json` length ≤ 256.

- [ ] **Task 4.3: Write `acts/act_03_hot_path_drift.py`** (Phases 8, 9, 10, 11). 3 edits on `ts_basic` across Edit/Write/NotebookEdit; verify match_quality envelope; trigger drift; refresh. Prompt from spec line 234-245. Runner-side: parse `session.hook_events` for 3 PreToolUse events with distinct matcher; verify `<chameleon-context>` advisory has `match_quality` + `sub_buckets` + canonical witness.

- [ ] **Task 4.4: Write `acts/act_04_v060_ux_bundle.py`** (Phases 12, 13, 14). auto_refresh + canonical_ref + trust.auto_preserve_when. Prompt from spec line 249-258. Runner-side: use `ctx.setup_git_shim(5.0)` for the 2-second timeout sub-test; verify `auto_refresh.log` mode 0o600 + size ≤ 64KB; verify `.auto_refresh_cooldown` mtime > Popen call mtime.

- [ ] **Task 4.5: Write `acts/act_05_teach_status_doctor.py`** (Phases 16, 17, 18). Structured teach + slug boundary tests + status + corrupted-canonicals doctor. Prompt from spec line 263-275. Runner-side: read `working/ts_basic/.chameleon/idioms.md`, verify total size ≤ 200KB; verify `Language: typescript` frontmatter.

- [ ] **Task 4.6: Write `acts/act_06_suppression_callout.py`** (Phases 19, 20, 23). Pause + disable + 4-level precedence + HMAC tampering + callout-detector 7 patterns. Prompt from spec line 279-293. Runner-side: read `.session_disabled.<sid>` markers via Bash, verify HMAC validity using `mcp/chameleon_mcp/exec_log.py` helpers.

- [ ] **Task 4.7: Write `acts/act_07_rails_parity.py`** (Phase 21). Rails fixture init + 3 edits + refresh + teach + language_hint hybrid. Prompt from spec line 297-307. Runner-side: verify `rules.json` has `rubocop` key; verify `working/ts_with_rails_sidecar` `language_hint` surfaces in SessionStart.

- [ ] **Task 4.8: Write `acts/act_08_hooks_security_sanitization.py`** (Phases 22, 24, 25, 26). PostToolUse on Bash+Edit+Write+NotebookEdit; symlink refusal; adversarial canonicals; 5MB boundary. Prompt from spec line 311-326. Runner-side: read `${TMPDIR}/.chameleon_exec_log/<repo_id>/<sid>.jsonl`, verify HMAC sig matches.

- [ ] **Task 4.9: Write `acts/act_09_schema_atomicity_concurrency.py`** (Phases 27, 28, 29, 30, 31, 32). Schema migration (v0.3/v0.4/v99); deterministic orphan-txn cleanup with dead PID; concurrent refresh; brace expansion; merge driver; monorepo aggregation. Prompt from spec line 330-352. Runner-side: verify monorepo workspaces[*].archetypes[A].cluster_size_total = sum of per-workspace cluster_size.

- [ ] **Task 4.10: Write `acts/act_10_daemon_observability_resilience.py`** (Phases 33, 34, 35, 36). Daemon socket + serial queue + idle shutdown; metrics.jsonl per-call fields; log rotation; hook fail-open. Prompt from spec line 356-380. Runner-side: read `metrics.jsonl`, verify each entry has all required fields; verify `auto_refresh.log` truncates on each spawn.

- [ ] **Task 4.11: Write `acts/act_11_uninstall_cleanup.py`** (Phase 37). Uninstall + cleanup + isolation re-verify. Prompt from spec line 384-389. Runner-side: assert `~/.local/share/chameleon/` and `~/.claude/hooks/.exec_hmac.key` still exist (developer's pre-existing data untouched); assert `<run_dir>/chameleon_data/` was wiped.

For each act:

- [ ] **Step 1: Write the act module** (paste prompt skeleton from spec; embed runner-side assertions per the 38-phase table).
- [ ] **Step 2: Commit individually** with message `Add act N: <one-line summary>`.

After all 12 acts exist:

- [ ] **Step 3: Verify all 12 act modules importable**

Run: `cd /Users/crisn/Documents/Projects/chameleon && PYTHONPATH=. mcp/.venv/bin/python -c "import tests.journey.runner; r = tests.journey.runner; [r.importlib.import_module(f'tests.journey.acts.act_{a[0]}') for a in r._ACTS]; print('all 12 acts importable')"`

Expected: `all 12 acts importable`.

- [ ] **Step 4: Commit if any import fixes needed**

```bash
git add tests/journey/acts/
git commit -m "Wire all 12 acts into runner orchestration"
```

---

## Phase 5: Skill rename + cleanup

This phase deletes the old `tests/` tree and renames the dogfood skill. The runner must already be working (Phase 4 complete) before this phase runs.

### Task 5.1: Rename chameleon-dogfood skill to chameleon-journey

**Files:**
- Move: `skills/chameleon-dogfood/SKILL.md` → `skills/chameleon-journey/SKILL.md`

- [ ] **Step 1: Move directory**

```bash
git mv skills/chameleon-dogfood skills/chameleon-journey
```

- [ ] **Step 2: Rewrite SKILL.md**

Edit `skills/chameleon-journey/SKILL.md`:

```markdown
---
name: chameleon-journey
description: Use when the user explicitly invokes /chameleon-journey to run the comprehensive real-world journey harness against the chameleon plugin
---

# /chameleon-journey

Run the journey harness at `tests/journey/`. The harness verifies chameleon's full lifecycle by spawning real `claude -p` subprocesses against committed seed fixtures.

## Defaults

Full run: 12 acts, ~$25 cost, ~65 min runtime, ~$35 hard budget cap.

## Run

From the chameleon repo root:

```bash
mcp/.venv/bin/python -m tests.journey.runner
```

Variations:

- `--list`: show acts + phase coverage, exit 0.
- `--dry-run`: run preflight only (claude on PATH, git >= 2.28, fixtures present, mcp/.venv), exit before any Claude spawn.
- `--max-budget-usd N`: pre-flight + mid-run abort if projected cost exceeds N (default 35).
- `--results-dir DIR`: override per-run output dir (default `tests/journey/results/`).

## Output

- stderr: per-act `[ACT N] ...` markers + cost + duration.
- `tests/journey/results/journey_<ts>/run.json` + `run.md`: per-act + per-phase results.
- `tests/journey/results/journey_<ts>/snapshots/<act>/<phase>/`: captured state per phase for post-mortem.

## Notes

The full run requires:
- `claude` CLI on PATH with API access.
- git >= 2.28 (for `git init --initial-branch=main`).
- `mcp/.venv/bin/python` (run `cd mcp && uv sync` if missing).

The harness writes ALL state to a per-run dir; the developer's own `~/.local/share/chameleon/` is never touched.
```

- [ ] **Step 3: Commit**

```bash
git add skills/chameleon-journey/
git rm -r skills/chameleon-dogfood/ 2>/dev/null || true
git commit -m "Rename /chameleon-dogfood skill to /chameleon-journey"
```

### Task 5.2: Delete old tests/ tree (dogfood, e2e, hook_evals, calibration)

**Files:**
- Delete: `tests/dogfood/`, `tests/e2e/`, `tests/hook_evals/`, `tests/calibration/`, `tests/fixtures/`

- [ ] **Step 1: Delete dirs**

```bash
git rm -rf tests/dogfood tests/e2e tests/hook_evals tests/calibration tests/fixtures
```

- [ ] **Step 2: Verify journey/ untouched**

Run: `ls tests/`
Expected: `__pycache__/`, `journey/`, and the remaining `*_test.py` files (deleted in next task).

- [ ] **Step 3: Commit**

```bash
git commit -m "Delete tests/dogfood + tests/e2e + tests/hook_evals + tests/calibration + tests/fixtures"
```

### Task 5.3: Delete all tests/*_test.py + tests/run_all_orders.py

**Files:**
- Delete: every `tests/*_test.py` (~95 files) + `tests/run_all_orders.py` + `tests/_test_config.py` + `tests/test-helpers.sh` + `tests/skill_triggering_test.sh`

- [ ] **Step 1: Delete unit test files**

```bash
git rm tests/*_test.py tests/run_all_orders.py tests/_test_config.py tests/test-helpers.sh tests/skill_triggering_test.sh
rm -rf tests/__pycache__
```

- [ ] **Step 2: Verify**

Run: `ls tests/`
Expected: only `journey/` (and possibly an `__init__.py`; remove if present).

- [ ] **Step 3: Commit**

```bash
git commit -m "Delete 95+ legacy unit tests and run_all_orders harness"
```

### Task 5.4: Update CLAUDE.md test commands section

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Replace the test commands section**

Find the "Working on this codebase" section in CLAUDE.md. Replace the test commands subsection with:

```markdown
### Run the journey harness

```bash
mcp/.venv/bin/python -m tests.journey.runner               # full run (~$25, ~65 min)
mcp/.venv/bin/python -m tests.journey.runner --list        # list acts
mcp/.venv/bin/python -m tests.journey.runner --dry-run     # preflight only, no Claude spawn
mcp/.venv/bin/python -m tests.journey.runner --max-budget-usd 20
```

The journey harness drives real `claude -p` subprocesses against committed seed fixtures. Run before each release. All state is isolated to a per-run dir under `tests/journey/results/`; the developer's own `~/.local/share/chameleon/` is never touched.

### Run unit tests for the harness library

```bash
cd /Users/crisn/Documents/Projects/chameleon && PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/journey/harness/tests/ -v
```

These verify the harness library itself (context, checkpoints, expect, fixtures setup). They do NOT test chameleon; that's the journey runner's job.
```

- [ ] **Step 2: Remove obsolete test parameterization section**

Remove the `.env` `CHAMELEON_TEST_TS_REPO` / `CHAMELEON_TEST_RUBY_REPO` env var documentation; fixtures are now committed under `tests/journey/fixtures/`.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "Update CLAUDE.md test commands for journey harness"
```

### Task 5.5: Verify bump-version.sh has no obsolete tests/ touchpoints

- [ ] **Step 1: Check**

Run: `grep -nE "tests/(dogfood|e2e|hook_evals|calibration|.*_test)" scripts/bump-version.sh`
Expected: no matches.

- [ ] **Step 2: If any matches, edit them out + commit. Otherwise skip.**

---

## Phase 6: Pre-merge audit + smoke test

### Task 6.1: Run CHAMELEON_PLUGIN_DATA audit grep

- [ ] **Step 1: Run the audit**

Run: `grep -rn "Path.home()\|.local/share/chameleon\|.claude/hooks" mcp/chameleon_mcp/ | sort`

- [ ] **Step 2: Verify each match honors an env override**

For each match, confirm it either:
- Routes through `plugin_paths.plugin_data_dir()` (which checks `CHAMELEON_PLUGIN_DATA`), OR
- Honors `CHAMELEON_HMAC_KEY_PATH`, `CHAMELEON_HOOK_ERROR_LOG`, or `TMPDIR` as a fallback override.

Expected compliant sites (from round-3 review):
- `daemon.py` socket path
- `canonical_loader.py:117-119`
- `metrics.py:21-26`
- `exec_log.py:23-35`
- `index_db.py:30`
- `profile/trust.py:25-43`
- `tools.py:5421` (hook errors fallback)
- `hook_helper.py:85-88`

If any new callsite was added that hardcodes a home-dir path WITHOUT honoring an env override, fix it before merge.

- [ ] **Step 3: Document audit result**

If all sites compliant, no commit needed. If any fixes applied to chameleon code, commit with message describing what was fixed.

### Task 6.2: Full journey smoke run

- [ ] **Step 1: Run the full harness**

Run: `cd /Users/crisn/Documents/Projects/chameleon && mcp/.venv/bin/python -m tests.journey.runner --max-budget-usd 35`

Expected: 12 acts run, total cost ~$20-25, runtime ~50-65 min. Exit code 0 if every phase PASSes; non-zero if any FAIL/ERROR.

- [ ] **Step 2: Inspect run.md**

Open `tests/journey/results/journey_<latest>/run.md`. Verify every phase reports PASS. Investigate any FAIL/ERROR.

- [ ] **Step 3: If failures, iterate**

For each failed phase:
1. Read the snapshot at `tests/journey/results/journey_<ts>/snapshots/<act>/<phase>/`.
2. Read the Claude transcript at `tests/journey/results/journey_<ts>/transcripts/<act>.txt`.
3. Diagnose: is it a chameleon bug, a fixture issue, or a harness bug?
4. Fix and re-run.

- [ ] **Step 4: When all green, commit any remaining fixes**

```bash
git add -A
git commit -m "Polish journey harness based on first full run"
```

---

## Self-Review

### 1. Spec coverage check

| Spec section | Plan task(s) |
|---|---|
| File structure (lines 130-176) | Task 0.2, 1.1-1.10 |
| Test isolation strategy (lines 36-67) | Task 1.1, 1.4 |
| Failure attribution (lines 69-94) | Task 1.2 |
| Cost control (lines 96-110) | Task 3.1 |
| Time-driven phase mechanics (lines 162-172) | Task 1.1 (`fast_forward_marker`), exercised in Act 6 + 8 + 10 |
| 12 acts (lines 178-396) | Task 4.0-4.11 |
| 38 phases inventory | Covered transitively via acts |
| JourneyContext API (lines 442-500) | Task 1.1, 1.9, 1.4 |
| Runner CLI (lines 521-540) | Task 3.1 |
| Output format (lines 542-571) | Task 3.1 `_write_outputs` |
| Migration plan (lines 590-610) | Task 5.1-5.5 |
| Migration step 9 audit | Task 6.1 |

All sections covered. ✓

### 2. Placeholder scan

Searched the plan for "TBD", "TODO", "implement later", "fill in details", "Similar to Task". Found:
- Tasks 4.2-4.11 use a compressed pattern with prompt-from-spec references rather than full prompt text inline. This is acceptable because the spec is the source of truth and is committed. The plan describes the SHAPE of each act file fully (skeleton in Task 4.1), and lists specific assertions per act. An engineer working from this plan reads the act's spec section + the Task 4.1 skeleton to assemble each act.

No TBDs or "fill in later" placeholders in foundational tasks (Phase 0-3). The compressed format in Phase 4 is by design (12 acts share 95% structure).

### 3. Type consistency

- `JourneyContext` defined in Task 1.1; used by every act + runner in Task 3.1.
- `PhaseOutcome` defined in Task 1.2; used by `ActResult` (Task 3.2) + each act (Task 4.0+).
- `ActResult` defined in Task 3.2; returned by every act module.
- `ClaudeSession`, `HookEvent`, `parse_stream_json` defined in Task 1.8; used by every act via `spawn_claude`.
- `ShimHandle`, `setup_git_shim` defined in Task 1.9; used by Act 4 (Task 4.4).
- `setup_fixture` defined in Task 1.4; used by `runner.py` in Task 3.1.

All types consistent across tasks. ✓

### 4. Scope check

This is one plan because the harness library + fixtures + runner + acts are interdependent and the migration is single-PR per the spec. The plan decomposes into 6 phases (setup, library, fixtures, runner, acts, cleanup+audit) where each phase produces something testable on its own:
- Phase 1 produces a tested harness library (pytest passes).
- Phase 2 produces validatable seed fixtures.
- Phase 3 produces a working runner skeleton (`--list` + `--dry-run`).
- Phase 4 produces 12 act modules, each runnable in isolation.
- Phase 5 deletes the old tests/.
- Phase 6 runs the full smoke + audit.

Scope is appropriate for one plan. ✓

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-21-journey-harness.md`. Two execution options:

**1. Subagent-Driven (recommended):** I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution:** Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
