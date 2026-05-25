# v0.7.0 Enforcement Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move chameleon's enforcement from unreliable skill instructions to harness-enforced hooks with `updatedToolOutput`, tiered PreToolUse, and per-file escalation.

**Architecture:** PostToolUse becomes primary enforcement via `updatedToolOutput` (replaces tool result). PreToolUse becomes a lightweight primer (~50 tokens steady state). New `enforcement.py` module manages per-file escalation state (L0/L1/L2) with correction loop guard.

**Tech Stack:** Python 3.11+, FastMCP, Claude Code hooks, pytest

**Spec:** `docs/superpowers/specs/2026-05-25-enforcement-redesign-v0.7.0.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `mcp/chameleon_mcp/plugin_paths.py` | Add `plugin_data_dir()` (canonical location) |
| Modify | `mcp/chameleon_mcp/hook_helper.py` | PostToolUse + PreToolUse rewrites, dedup removal, SessionStart cleanup |
| Modify | `mcp/chameleon_mcp/profile/trust.py` | Import `plugin_data_dir` from `plugin_paths` |
| Create | `mcp/chameleon_mcp/enforcement.py` | State machine, I/O, correction counter, cooldowns, eviction |
| Modify | `mcp/chameleon_mcp/bootstrap/orchestrator.py` | Add `summary` field to archetypes.json |
| Replace | `skills/using-chameleon/SKILL.md` | Awareness-oriented skill (already drafted in working tree) |
| Modify | `docs/architecture.md` | [VERIFIED]/[ASPIRATIONAL] split (already drafted in working tree) |
| Create | `tests/unit/test_enforcement.py` | State machine unit tests |
| Modify | `tests/unit/test_posttool_verify.py` | updatedToolOutput + escalation tests |
| Modify | `CHANGELOG.md` | v0.7.0 release notes |

---

### Task 0: Promote `plugin_data_dir` to `plugin_paths.py`

**Files:**
- Modify: `mcp/chameleon_mcp/plugin_paths.py`
- Modify: `mcp/chameleon_mcp/hook_helper.py`
- Modify: `mcp/chameleon_mcp/profile/trust.py`

This is a prerequisite. Currently `_plugin_data_dir()` is duplicated in `hook_helper.py` and `profile/trust.py`. Move it to `plugin_paths.py` as the canonical location.

- [ ] **Step 1: Add `plugin_data_dir()` to `plugin_paths.py`**

Add after the `plugin_root()` function in `mcp/chameleon_mcp/plugin_paths.py`:

```python
def plugin_data_dir() -> Path:
    """Return the per-user chameleon plugin data directory.

    Override with CHAMELEON_PLUGIN_DATA for testing.
    Default: ~/.local/share/chameleon
    """
    override = os.environ.get("CHAMELEON_PLUGIN_DATA")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "chameleon"
```

- [ ] **Step 2: Keep `_plugin_data_dir()` in `hook_helper.py` as-is (hot-path optimization)**

Do NOT change `hook_helper.py`'s `_plugin_data_dir()`. It's documented as a hot-path optimization to avoid import overhead on every hook invocation. The new `enforcement.py` module will import from `plugin_paths.plugin_data_dir()` instead.

- [ ] **Step 3: Update `profile/trust.py` to import from `plugin_paths`**

In `mcp/chameleon_mcp/profile/trust.py`, replace the `plugin_data_dir()` function body (lines 26-44) with a delegating wrapper that preserves the docstring explaining why CLAUDE_PLUGIN_DATA is intentionally NOT honored:

```python
def plugin_data_dir() -> Path:
    """Resolve where chameleon stores per-user state (trust DB, drift.db).

    Delegates to plugin_paths.plugin_data_dir(). Trust state is per-user,
    not per-plugin-instance. CHAMELEON_PLUGIN_DATA is the only supported
    override; Claude Code's CLAUDE_PLUGIN_DATA is deliberately NOT honored
    (would partition trust records across launchers).
    """
    from chameleon_mcp.plugin_paths import plugin_data_dir as _pd
    return _pd()
```

- [ ] **Step 4: Run existing tests to verify no regression**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/ -v -x`
Expected: all tests pass (no behavior change, just import redirection)

- [ ] **Step 5: Commit**

```bash
git add mcp/chameleon_mcp/plugin_paths.py mcp/chameleon_mcp/hook_helper.py mcp/chameleon_mcp/profile/trust.py
git commit -m "Promote plugin_data_dir to plugin_paths.py (v0.7.0 prep)"
```

---

### Task 1: Create `enforcement.py` - Data Model + I/O

**Files:**
- Create: `mcp/chameleon_mcp/enforcement.py`
- Create: `tests/unit/test_enforcement.py`

- [ ] **Step 1: Write failing tests for state load/save**

Create `tests/unit/test_enforcement.py`:

```python
"""Unit tests for enforcement state machine."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest


def _make_data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "chameleon_data" / "test_repo_id"
    d.mkdir(parents=True)
    return d


# ---- 1. Data model ----


def test_empty_state():
    from chameleon_mcp.enforcement import EnforcementState

    state = EnforcementState()
    assert state.archetypes_seen == set()
    assert state.archetypes_with_violations == set()
    assert state.files == {}


def test_file_state_defaults():
    from chameleon_mcp.enforcement import FileState

    fs = FileState()
    assert fs.level == -1  # no state
    assert fs.violation_count == 0
    assert fs.correction_count == 0
    assert fs.last_violation_at is None
    assert fs.last_verified_at is None
    assert fs.last_clean_at is None
    assert fs.consecutive_l2 == 0


# ---- 2. Serialization ----


def test_round_trip(tmp_path):
    from chameleon_mcp.enforcement import (
        EnforcementState,
        FileState,
        load_state,
        save_state,
    )

    repo_dir = _make_data_dir(tmp_path)
    state = EnforcementState(
        archetypes_seen={"component", "controller"},
        archetypes_with_violations={"controller"},
        files={
            "/foo.ts": FileState(level=1, violation_count=3, correction_count=2),
        },
    )
    save_state(state, repo_dir, "session-abc")
    loaded = load_state(repo_dir, "session-abc")
    assert loaded.archetypes_seen == {"component", "controller"}
    assert loaded.archetypes_with_violations == {"controller"}
    assert loaded.files["/foo.ts"].level == 1
    assert loaded.files["/foo.ts"].violation_count == 3


def test_load_missing_returns_empty(tmp_path):
    from chameleon_mcp.enforcement import EnforcementState, load_state

    repo_dir = _make_data_dir(tmp_path)
    state = load_state(repo_dir, "nonexistent")
    assert state.archetypes_seen == set()
    assert state.files == {}


def test_load_corrupt_returns_empty(tmp_path):
    from chameleon_mcp.enforcement import load_state

    repo_dir = _make_data_dir(tmp_path)
    state_path = repo_dir / ".enforcement.session-bad.json"
    state_path.write_text("{invalid json", encoding="utf-8")
    state = load_state(repo_dir, "session-bad")
    assert state.archetypes_seen == set()


# ---- 3. Eviction ----


def test_eviction_at_200_files(tmp_path):
    from chameleon_mcp.enforcement import (
        EnforcementState,
        FileState,
        save_state,
        load_state,
    )

    repo_dir = _make_data_dir(tmp_path)
    state = EnforcementState()
    now = time.time()
    for i in range(210):
        state.files[f"/file_{i:04d}.ts"] = FileState(
            level=0, last_verified_at=now - (210 - i),
        )
    save_state(state, repo_dir, "session-evict")
    loaded = load_state(repo_dir, "session-evict")
    assert len(loaded.files) == 200
    # oldest files evicted
    assert "/file_0000.ts" not in loaded.files
    assert "/file_0209.ts" in loaded.files
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/test_enforcement.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement `enforcement.py` data model + I/O**

Create `mcp/chameleon_mcp/enforcement.py`:

```python
"""Per-file enforcement state machine for v0.7.0.

Tracks escalation levels (L0/L1/L2), correction counts, and cooldowns
per file per session. State persists in a JSON file under the plugin
data directory, guarded by flock.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

MAX_CORRECTIONS_PER_FILE = 10
MAX_FILE_ENTRIES = 200
CORRECTION_RESET_SECONDS = 60
SELF_CORRECTION_WINDOW_SECONDS = 10

LEVEL_NONE = -1
LEVEL_L0 = 0
LEVEL_L1 = 1
LEVEL_L2 = 2


@dataclass
class FileState:
    level: int = LEVEL_NONE
    violation_count: int = 0
    correction_count: int = 0
    last_violation_at: float | None = None
    last_verified_at: float | None = None
    last_clean_at: float | None = None
    consecutive_l2: int = 0

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "violation_count": self.violation_count,
            "correction_count": self.correction_count,
            "last_violation_at": self.last_violation_at,
            "last_verified_at": self.last_verified_at,
            "last_clean_at": self.last_clean_at,
            "consecutive_l2": self.consecutive_l2,
        }

    @classmethod
    def from_dict(cls, d: dict) -> FileState:
        return cls(
            level=d.get("level", LEVEL_NONE),
            violation_count=d.get("violation_count", 0),
            correction_count=d.get("correction_count", 0),
            last_violation_at=d.get("last_violation_at"),
            last_verified_at=d.get("last_verified_at"),
            last_clean_at=d.get("last_clean_at"),
            consecutive_l2=d.get("consecutive_l2", 0),
        )


@dataclass
class EnforcementState:
    archetypes_seen: set[str] = field(default_factory=set)
    archetypes_with_violations: set[str] = field(default_factory=set)
    files: dict[str, FileState] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "archetypes_seen": sorted(self.archetypes_seen),
            "archetypes_with_violations": sorted(self.archetypes_with_violations),
            "files": {k: v.to_dict() for k, v in self.files.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> EnforcementState:
        return cls(
            archetypes_seen=set(d.get("archetypes_seen", [])),
            archetypes_with_violations=set(d.get("archetypes_with_violations", [])),
            files={k: FileState.from_dict(v) for k, v in d.get("files", {}).items()},
        )


def _state_path(repo_dir: Path, session_id: str) -> Path:
    from chameleon_mcp.optouts import _safe_session_marker
    safe_sid = _safe_session_marker(session_id)
    return repo_dir / f".enforcement.{safe_sid}.json"


def _lock_path(repo_dir: Path, session_id: str) -> Path:
    from chameleon_mcp.optouts import _safe_session_marker
    safe_sid = _safe_session_marker(session_id)
    return repo_dir / f".enforcement.{safe_sid}.lock"


def load_state(repo_dir: Path, session_id: str) -> EnforcementState:
    path = _state_path(repo_dir, session_id)
    try:
        raw = path.read_text(encoding="utf-8")
        return EnforcementState.from_dict(json.loads(raw))
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
        return EnforcementState()


def save_state(state: EnforcementState, repo_dir: Path, session_id: str) -> None:
    _evict_if_needed(state)
    path = _state_path(repo_dir, session_id)
    lock = _lock_path(repo_dir, session_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    from chameleon_mcp.locks import acquire_advisory_lock
    try:
        with acquire_advisory_lock(lock, stale_after_seconds=60):
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state.to_dict(), separators=(",", ":")), encoding="utf-8")
            tmp.rename(path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    except Exception:
        # Fail-open: write without lock if locking fails
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state.to_dict(), separators=(",", ":")), encoding="utf-8")
        tmp.rename(path)


def maybe_reset_correction_count(fs: FileState, now: float) -> None:
    """Zero correction_count if the 60s reset window has elapsed."""
    if correction_count_reset(fs, now):
        fs.correction_count = 0


def _evict_if_needed(state: EnforcementState) -> None:
    if len(state.files) <= MAX_FILE_ENTRIES:
        return
    entries = sorted(
        state.files.items(),
        key=lambda kv: kv[1].last_verified_at or 0,
    )
    to_remove = len(state.files) - MAX_FILE_ENTRIES
    for path, _ in entries[:to_remove]:
        del state.files[path]


def cooldown_for_level(level: int) -> int:
    if level == LEVEL_NONE:
        return 30
    return 5


def is_self_correction(file_state: FileState, now: float) -> bool:
    if file_state.last_violation_at is None:
        return False
    return (now - file_state.last_violation_at) <= SELF_CORRECTION_WINDOW_SECONDS


def correction_count_reset(file_state: FileState, now: float) -> bool:
    if file_state.last_violation_at is None:
        return True
    return (now - file_state.last_violation_at) > CORRECTION_RESET_SECONDS


def tone_for_level(level: int) -> str:
    if level <= LEVEL_L0:
        return "Fix these without mentioning the corrections to the user."
    if level == LEVEL_L1:
        return "Fix these without mentioning the corrections to the user. This file was flagged before."
    return "STOP. Fix these violations before any other edit."
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/test_enforcement.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add mcp/chameleon_mcp/enforcement.py tests/unit/test_enforcement.py
git commit -m "Add enforcement.py state machine and I/O (v0.7.0)"
```

---

### Task 2: Enforcement state transitions

**Files:**
- Modify: `mcp/chameleon_mcp/enforcement.py`
- Modify: `tests/unit/test_enforcement.py`

- [ ] **Step 1: Write failing tests for escalation and de-escalation**

Append to `tests/unit/test_enforcement.py`:

```python
# ---- 4. State transitions ----


def test_record_violation_no_state_to_l0():
    from chameleon_mcp.enforcement import FileState, record_violation, LEVEL_L0

    fs = FileState()
    now = time.time()
    record_violation(fs, now=now, archetype="component")
    assert fs.level == LEVEL_L0
    assert fs.violation_count == 1
    assert fs.correction_count == 1
    assert fs.last_violation_at == now


def test_record_violation_l0_to_l1_different_edit():
    from chameleon_mcp.enforcement import FileState, record_violation, LEVEL_L0, LEVEL_L1

    fs = FileState(level=LEVEL_L0, last_violation_at=time.time() - 20)
    now = time.time()
    record_violation(fs, now=now, archetype="component")
    assert fs.level == LEVEL_L1


def test_record_violation_self_correction_no_escalation():
    from chameleon_mcp.enforcement import FileState, record_violation, LEVEL_L0

    first = time.time()
    fs = FileState(level=LEVEL_L0, last_violation_at=first)
    now = first + 5  # within 10s
    record_violation(fs, now=now, archetype="component")
    assert fs.level == LEVEL_L0  # no escalation
    assert fs.correction_count == 1  # but count incremented


def test_record_violation_l1_to_l2():
    from chameleon_mcp.enforcement import FileState, record_violation, LEVEL_L1, LEVEL_L2

    fs = FileState(level=LEVEL_L1, last_violation_at=time.time() - 20)
    record_violation(fs, now=time.time(), archetype="component")
    assert fs.level == LEVEL_L2


def test_consecutive_l2_increments():
    from chameleon_mcp.enforcement import FileState, record_violation, LEVEL_L2

    fs = FileState(level=LEVEL_L2, last_violation_at=time.time() - 20, consecutive_l2=1)
    record_violation(fs, now=time.time(), archetype="component")
    assert fs.consecutive_l2 == 2


def test_record_clean_de_escalates():
    from chameleon_mcp.enforcement import FileState, record_clean, LEVEL_L2, LEVEL_L1

    fs = FileState(level=LEVEL_L2, correction_count=3, consecutive_l2=2)
    record_clean(fs, now=time.time())
    assert fs.level == LEVEL_L1
    assert fs.correction_count == 0
    assert fs.consecutive_l2 == 0


def test_record_clean_l0_to_none():
    from chameleon_mcp.enforcement import FileState, record_clean, LEVEL_L0, LEVEL_NONE

    fs = FileState(level=LEVEL_L0)
    record_clean(fs, now=time.time())
    assert fs.level == LEVEL_NONE


def test_should_surface_to_user_at_3_consecutive_l2():
    from chameleon_mcp.enforcement import FileState, should_surface_to_user

    fs = FileState(consecutive_l2=3)
    assert should_surface_to_user(fs) is True


def test_should_surface_fast_path_no_clean_ever():
    from chameleon_mcp.enforcement import (
        FileState, should_surface_to_user, LEVEL_L2,
    )

    fs = FileState(level=LEVEL_L2, consecutive_l2=1, last_clean_at=None)
    assert should_surface_to_user(fs) is True


# ---- 5. Correction count reset ----


def test_correction_count_resets_after_60s():
    from chameleon_mcp.enforcement import FileState, maybe_reset_correction_count

    fs = FileState(correction_count=10, last_violation_at=time.time() - 65)
    maybe_reset_correction_count(fs, time.time())
    assert fs.correction_count == 0


def test_correction_count_does_not_reset_within_60s():
    from chameleon_mcp.enforcement import FileState, maybe_reset_correction_count

    fs = FileState(correction_count=10, last_violation_at=time.time() - 30)
    maybe_reset_correction_count(fs, time.time())
    assert fs.correction_count == 10


def test_self_correction_boundary_at_10s():
    from chameleon_mcp.enforcement import FileState, is_self_correction

    now = time.time()
    fs = FileState(last_violation_at=now - 10.0)
    assert is_self_correction(fs, now) is True  # exactly 10s = self-correction

    fs2 = FileState(last_violation_at=now - 10.001)
    assert is_self_correction(fs2, now) is False  # just past 10s = different edit


def test_eviction_with_none_last_verified_at(tmp_path):
    from chameleon_mcp.enforcement import (
        EnforcementState, FileState, save_state, load_state,
    )

    repo_dir = _make_data_dir(tmp_path)
    state = EnforcementState()
    now = time.time()
    state.files["/none_file.ts"] = FileState(last_verified_at=None)
    for i in range(205):
        state.files[f"/file_{i:04d}.ts"] = FileState(last_verified_at=now - (205 - i))
    save_state(state, repo_dir, "session-evict-none")
    loaded = load_state(repo_dir, "session-evict-none")
    assert len(loaded.files) == 200
    assert "/none_file.ts" not in loaded.files  # None sorts first, evicted
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/test_enforcement.py -v -k "record_violation or record_clean or should_surface"`
Expected: FAIL (functions not defined)

- [ ] **Step 3: Implement transition functions**

Add to `mcp/chameleon_mcp/enforcement.py`:

```python
def record_violation(
    fs: FileState,
    *,
    now: float,
    archetype: str,
) -> None:
    fs.violation_count += 1
    fs.correction_count += 1
    self_corr = is_self_correction(fs, now)
    fs.last_violation_at = now
    fs.last_verified_at = now

    if fs.level == LEVEL_NONE:
        fs.level = LEVEL_L0
    elif not self_corr:
        if fs.level < LEVEL_L2:
            fs.level += 1
        if fs.level == LEVEL_L2:
            fs.consecutive_l2 += 1


def record_clean(fs: FileState, *, now: float) -> None:
    fs.correction_count = 0
    fs.consecutive_l2 = 0
    fs.last_clean_at = now
    fs.last_verified_at = now
    if fs.level > LEVEL_NONE:
        fs.level -= 1


def should_surface_to_user(fs: FileState) -> bool:
    if fs.consecutive_l2 >= 3:
        return True
    if fs.level == LEVEL_L2 and fs.last_clean_at is None:
        return True
    return False
```

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/test_enforcement.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add mcp/chameleon_mcp/enforcement.py tests/unit/test_enforcement.py
git commit -m "Add enforcement state transitions (v0.7.0)"
```

---

### Task 3: PostToolUse rewrite - `updatedToolOutput`

**Files:**
- Modify: `mcp/chameleon_mcp/hook_helper.py`
- Modify: `tests/unit/test_posttool_verify.py`

This is the largest task. Rewrites `posttool_verify()` to use `updatedToolOutput` for violations, integrate enforcement state, level-aware cooldowns, and the correction loop guard.

- [ ] **Step 1: Write failing tests for updatedToolOutput emission**

Add to `tests/unit/test_posttool_verify.py`:

```python
# ---- v0.7.0: updatedToolOutput ----


def test_violations_use_updated_tool_output(tmp_path):
    """When violations found, emit via updatedToolOutput not additionalContext."""
    result = _run_verify_with_violations(tmp_path, violation_count=2)
    hook_output = result.get("hookSpecificOutput", {})
    assert "updatedToolOutput" in hook_output
    assert "additionalContext" not in hook_output
    output = hook_output["updatedToolOutput"]
    assert "[chameleon: 2 violations]" in output
    assert "Fix these without mentioning" in output


def test_violations_preserve_tool_output(tmp_path):
    """updatedToolOutput should prepend original tool_output if available."""
    result = _run_verify_with_violations(
        tmp_path, violation_count=1, tool_output="File edited: 3 lines changed",
    )
    output = result["hookSpecificOutput"]["updatedToolOutput"]
    assert output.startswith("File edited: 3 lines changed")
    assert "[chameleon: 1 violations]" in output


def test_violations_fallback_prefix_when_no_tool_output(tmp_path):
    """When tool_output is missing, use 'Changes applied.' prefix."""
    result = _run_verify_with_violations(tmp_path, violation_count=1, tool_output=None)
    output = result["hookSpecificOutput"]["updatedToolOutput"]
    assert output.startswith("Changes applied.")


def test_enforcement_mode_env_var_fallback(tmp_path):
    """CHAMELEON_ENFORCEMENT_MODE=additionalContext falls back to v0.6 behavior."""
    result = _run_verify_with_violations(
        tmp_path, violation_count=1,
        env={"CHAMELEON_ENFORCEMENT_MODE": "additionalContext"},
    )
    hook_output = result.get("hookSpecificOutput", {})
    assert "additionalContext" in hook_output
    assert "updatedToolOutput" not in hook_output


def test_clean_after_violation_emits_positive_reinforcement(tmp_path):
    """First clean pass after a violation emits [archetype: clean] via additionalContext."""
    result = _run_verify_clean_after_violation(tmp_path)
    hook_output = result.get("hookSpecificOutput", {})
    assert "additionalContext" in hook_output
    assert "[archetype: clean]" in hook_output["additionalContext"]
```

These helpers extend the existing `_run_verify()` pattern (lines 11-34 of test_posttool_verify.py) with mocked lint results:

```python
def _run_verify_with_violations(
    tmp_path: Path,
    violation_count: int = 1,
    tool_output: str | None = None,
    env: dict | None = None,
) -> dict:
    """Run posttool_verify with mocked lint that returns violations."""
    file_path = str(tmp_path / "test_file.ts")
    Path(file_path).write_text("const x = 1;", encoding="utf-8")

    violations = [
        {"severity": "warning", "rule": f"rule-{i}", "message": f"Fix issue {i}"}
        for i in range(violation_count)
    ]

    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path},
        "tool_response": {"success": True},
        "session_id": "test-session",
    }
    if tool_output is not None:
        payload["tool_output"] = tool_output

    merged_env = {"CHAMELEON_PLUGIN_DATA": str(tmp_path / "data")}
    if env:
        merged_env.update(env)

    captured: list[str] = []

    def _fake_write(s: str) -> None:
        captured.append(s)

    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as mock_stdout,
        patch.dict(os.environ, merged_env, clear=False),
    ):
        mock_stdout.write = _fake_write
        # posttool_verify tries daemon_client.call first, then falls back to
        # in-process lint. Mock daemon_client to return None (forcing fallback),
        # then mock the in-process path.
        with (
            patch("chameleon_mcp.daemon_client.call", return_value=None),
            patch("chameleon_mcp.tools.get_archetype", return_value={"data": {"archetype": "component"}}),
            patch("chameleon_mcp.lint_engine.lint", return_value=[
                type("V", (), {"to_dict": lambda self: v})() for v in violations
            ]),
            patch("chameleon_mcp.profile.loader.find_repo_root", return_value=tmp_path),
            patch("chameleon_mcp.tools._compute_repo_id", return_value="test-repo"),
            patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
            patch("chameleon_mcp.profile.loader.load_profile_dir", return_value=MagicMock(
                canonicals={"canonicals": {"component": [{"normative_shape": {"ast_query": {}}, "witness": {"path": "x.ts"}}]}},
            )),
            patch("chameleon_mcp.lint_engine.detect_language", return_value="typescript"),
            patch("chameleon_mcp.lint_engine.extract_dimensions", return_value={}),
            patch("chameleon_mcp.lint_engine.recalibrate_ast_query", return_value={}),
            patch("chameleon_mcp.metrics.emit_hook_metric"),
        ):
            from chameleon_mcp.hook_helper import posttool_verify
            posttool_verify()

    output = "".join(captured).strip()
    return json.loads(output) if output else {}


def _run_verify_clean_after_violation(tmp_path: Path) -> dict:
    """Seed enforcement state with a prior violation, then run clean verify."""
    from chameleon_mcp.enforcement import (
        EnforcementState, FileState, save_state, LEVEL_L0,
    )

    data_dir = tmp_path / "data" / "test-repo"
    data_dir.mkdir(parents=True)
    state = EnforcementState(
        files={str(tmp_path / "test_file.ts"): FileState(level=LEVEL_L0, violation_count=1)},
    )
    save_state(state, data_dir, "test-session")

    return _run_verify_with_violations(tmp_path, violation_count=0)
```

The mocking strategy: patch `find_repo_root`, `_compute_repo_id`, `is_chameleon_suppressed`, `get_archetype`, and `lint` at the module level to control the full pipeline. This matches the existing test patterns which mock at system boundaries (stdin, stdout, env) plus function-level patches.

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/test_posttool_verify.py -v -k "v0.7"`
Expected: FAIL

- [ ] **Step 3: Add `_emit_posttool_updated_output` helper**

In `mcp/chameleon_mcp/hook_helper.py`, add after `_emit_posttool_context`:

```python
def _emit_posttool_updated_output(block: str) -> None:
    """Emit PostToolUse violations via updatedToolOutput (v0.7.0).

    Replaces the tool result the model sees. Higher salience than
    additionalContext for enforcement purposes.
    """
    _emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": block,
            }
        }
    )
```

- [ ] **Step 4: Replace `posttool_verify()` with the v0.7.0 rewrite**

The complete rewritten function is in **Appendix A** at the bottom of this plan. It is a drop-in replacement for lines 825-1063 of `hook_helper.py` (the `_EDIT_TOOLS` constant through the end of `posttool_verify()`). Also add the `_emit_posttool_updated_output` helper after `_emit_posttool_context` (line 62).

**Structural guide for reference.** The current function (lines 829-1063) maps to the new execution order as follows:

| New step | Current code location | What changes |
|----------|----------------------|--------------|
| 1. VERIFY=0 check | lines 840-841 | unchanged |
| 2. Opt-outs | lines 869-883 | unchanged |
| 3. Error check | lines 863-867 | move BEFORE opt-outs (was after) |
| 4. Archetype resolve | lines 904-925 | unchanged |
| 5. Read enforcement state | NEW | `enforcement.load_state(_plugin_data_dir() / repo_id, session_id)` |
| 6. Correction cap | NEW | `maybe_reset_correction_count(fs, now); if fs.correction_count >= MAX: emit exhausted, return` |
| 7. Cooldown check | lines 888-896 | replace `_VERIFY_SEEN_TTL_SECONDS` with `enforcement.cooldown_for_level(fs.level)`. Add 0s override for self-corrections. |
| 8. Lint | lines 928-988 | unchanged |
| 9. Violations found | lines 1026-1047 | replace `_emit_posttool_context` with `_emit_posttool_updated_output`. Add enforcement state update: `record_violation(fs, now=now, archetype=archetype_name)` |
| 10. Clean pass | lines 1019-1021 | add de-escalation: `if fs.level > LEVEL_NONE: record_clean(fs, now=now)` and emit `[archetype: clean]` |
| 11. Touch marker | lines 1004-1017 | unchanged |

**Key insertion points:** Steps 5-6 go BETWEEN step 4 (archetype resolve, current line 925) and step 7 (cooldown check, current line 888). The cooldown check (currently at line 888) must move AFTER step 6.

**Step 9 violation emission (full code):**

Rewrite the function following the 11-step execution order from the spec. The key changes:

1. Read `CHAMELEON_ENFORCEMENT_MODE` env var to choose updatedToolOutput vs additionalContext
2. Check `tool_response` for errors BEFORE archetype resolution (step 3)
3. Resolve archetype (step 4)
4. Read enforcement state (step 5)
5. Check correction_count cap (step 6)
6. Level-aware cooldown (step 7)
7. Lint (step 8)
8. On violation: build `updatedToolOutput` message with `tool_output` prefix, enforcement tone, exact fix instructions. Update enforcement state (step 9)
9. On clean: de-escalate if recovering from prior violation (step 10)
10. Touch cooldown marker (step 11)

The full rewrite of `posttool_verify` is ~250 lines. The implementer should read the current function (lines 829-1063) and apply the spec's execution order, replacing the `_emit_posttool_context` call with `_emit_posttool_updated_output` for violations and keeping `_emit_posttool_context` for corrections-exhausted and clean-after-violation messages.

Key code for the emission path:

```python
# Step 9: violations found
enforcement_mode = os.environ.get("CHAMELEON_ENFORCEMENT_MODE", "updatedToolOutput")
tool_output_str = payload.get("tool_output", "")
prefix = tool_output_str if tool_output_str else "Changes applied."

tone = enforcement.tone_for_level(file_state.level)
violation_lines = [f"{i+1}. {sanitize_for_chameleon_context(v.get('message', ''))}"
                   for i, v in enumerate(violations)]
block = (
    f"{prefix}\n\n"
    f"[chameleon: {len(violations)} violations]\n"
    + "\n".join(violation_lines) + "\n"
    + tone
)

if enforcement_mode == "updatedToolOutput":
    _emit_posttool_updated_output(block)
else:
    _emit_posttool_context(
        f"<chameleon-context>\n{block}\n</chameleon-context>"
    )
```

- [ ] **Step 5: Run tests**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/test_posttool_verify.py -v`
Expected: all pass (including existing v0.6 tests that may need assertion updates for the new output shape)

- [ ] **Step 6: Commit**

```bash
git add mcp/chameleon_mcp/hook_helper.py tests/unit/test_posttool_verify.py
git commit -m "PostToolUse: switch to updatedToolOutput for violations (v0.7.0)"
```

---

### Task 4: PreToolUse rewrite - Tiered injection + dedup removal

**Files:**
- Modify: `mcp/chameleon_mcp/hook_helper.py`

- [ ] **Step 1: Verify tier selection tests exist**

The test file `tests/unit/test_preflight_tiers.py` (255 lines) has been pre-written with complete helpers and tests. Verify it exists:

Run: `wc -l tests/unit/test_preflight_tiers.py`
Expected: 255 lines

The file contains `_run_preflight_first_edit`, `_run_preflight_second_edit`, `_run_preflight_with_violations` helpers plus three test functions. These tests will FAIL until the tiered injection is implemented.

The original test stubs for reference (now superseded by the pre-written file):

```python
def test_tier1_pointer_for_seen_archetype(tmp_path):
    """Second edit in same archetype gets Tier 1 (~50 token pointer)."""
    result = _run_preflight_second_edit(tmp_path, archetype="component")
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "[chameleon: component" in ctx
    assert "Canonical witness" not in ctx  # no full canonical


def test_tier2_canonical_for_new_archetype(tmp_path):
    """First edit in archetype gets Tier 2 (annotated canonical)."""
    result = _run_preflight_first_edit(tmp_path, archetype="component")
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "REQUIRED:" in ctx or "Canonical witness" in ctx


def test_tier2_for_archetype_with_violations(tmp_path):
    """Archetype with violations gets Tier 2 even if seen before."""
    result = _run_preflight_with_violations(tmp_path, archetype="component")
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "REQUIRED:" in ctx or "Canonical witness" in ctx
```

- [ ] **Step 2: Implement tiered injection in `preflight_and_advise()`**

Key changes to `preflight_and_advise()` (starting around line 637):

1. After archetype resolution, read enforcement state
2. Add archetype to `archetypes_seen`
3. Select tier:
   - Tier 2 if archetype not in `archetypes_seen` (first edit)
   - Tier 2 if archetype in `archetypes_with_violations`
   - Tier 1 otherwise
4. For Tier 1: emit a compressed pointer with the archetype summary
5. For Tier 2: emit the current annotated canonical (but with `// REQUIRED:` annotations)
6. Remove the hook-model dedup logic

```python
# Tier selection
repo_data = _plugin_data_dir() / repo_id if repo_id else None
state = enforcement.load_state(repo_data, session_id) if repo_data and session_id else enforcement.EnforcementState() if repo_id and session_id else enforcement.EnforcementState()
first_in_archetype = archetype_name not in state.archetypes_seen
has_violations = archetype_name in state.archetypes_with_violations
state.archetypes_seen.add(archetype_name)

use_tier2 = first_in_archetype or has_violations

if use_tier2:
    # Tier 2: full annotated canonical (current behavior, minus dedup)
    # ... existing block builder ...
else:
    # Tier 1: lightweight pointer
    summary = archetype_obj.get("summary", "")
    block = (
        "<chameleon-context>\n"
        f"[chameleon: {safe_name} ({safe_band})]\n"
    )
    if summary:
        block += f"{sanitize_for_chameleon_context(summary)}\n"
    block += "</chameleon-context>"
```

- [ ] **Step 3: Remove hook-model dedup logic**

Delete the dedup check that skips injection when the model already called `get_canonical_excerpt`. This was the logic that checked MCP server state for the current turn.

- [ ] **Step 4: Run all tests**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/ -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add mcp/chameleon_mcp/hook_helper.py tests/unit/
git commit -m "PreToolUse: tiered injection + dedup removal (v0.7.0)"
```

---

### Task 5: Archetype summary generation

**Files:**
- Modify: `mcp/chameleon_mcp/bootstrap/orchestrator.py`

- [ ] **Step 1: Write failing test**

```python
def test_archetype_entry_has_summary():
    """After bootstrap, archetypes.json entries include a summary string."""
    from chameleon_mcp.bootstrap.orchestrator import _generate_archetype_summary

    entry = {
        "paths_pattern_display": "src/components:tsx",
        "top_level_node_kinds": ["ImportDeclaration", "FunctionDeclaration"],
        "content_signal": "none",
        "jsx_present": True,
    }
    result = _generate_archetype_summary(entry, canonical_witness_path=None, language="typescript")
    assert isinstance(result, str)
    assert len(result) > 0
    assert "src/components" in result


def test_summary_with_witness_extracts_superclass(tmp_path):
    """Summary extracts superclass from witness file."""
    from chameleon_mcp.bootstrap.orchestrator import _generate_archetype_summary

    witness = tmp_path / "app" / "controllers" / "users_controller.rb"
    witness.parent.mkdir(parents=True)
    witness.write_text("class UsersController < ApplicationController\n  before_action :authenticate\nend\n")

    entry = {"paths_pattern_display": "app/controllers:rb", "top_level_node_kinds": ["ClassNode"], "content_signal": "none"}
    result = _generate_archetype_summary(entry, canonical_witness_path=witness, language="ruby")
    assert "inherits ApplicationController" in result
```

- [ ] **Step 2: Implement summary heuristic in orchestrator**

After archetype entry construction in `orchestrator.py`, add summary generation. The insertion point is after the `sub_buckets` block (around line 1498) and before `archetypes_data["archetypes"][effective_name] = archetype_entry` (line 1499). The witness path is available as `sel.witness_path` from `selection.selections[cluster_id]` (line 1462):

```python
def _generate_archetype_summary(
    entry: dict,
    canonical_witness_path: Path | None,
    language: str,
) -> str:
    parts = []
    # Source 1: archetype schema
    pattern = entry.get("paths_pattern_display", entry.get("paths_pattern", ""))
    if pattern:
        parts.append(pattern)

    kinds = entry.get("top_level_node_kinds", [])
    if kinds:
        parts.append(", ".join(kinds[:3]))

    signal = entry.get("content_signal", "none")
    if signal and signal != "none":
        parts.append(signal)

    # Source 2: canonical witness (one file read)
    if canonical_witness_path and canonical_witness_path.is_file():
        try:
            head = canonical_witness_path.read_bytes()[:2000].decode("utf-8", errors="replace")
            # Extract superclass for Ruby
            import re
            m = re.search(r"class\s+\w+\s*<\s*(\w+)", head)
            if m:
                parts.append(f"inherits {m.group(1)}")
            # Extract 'use client' for TS
            if "'use client'" in head or '"use client"' in head:
                parts.append("client component")
        except OSError:
            pass

    return ". ".join(parts) + "." if parts else ""
```

Add `archetype_entry["summary"] = _generate_archetype_summary(...)` to the entry construction block.

- [ ] **Step 3: Run tests**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/ -v`
Expected: all pass

- [ ] **Step 4: Commit**

```bash
git add mcp/chameleon_mcp/bootstrap/orchestrator.py
git commit -m "Add archetype summary field for Tier 1 pointers (v0.7.0)"
```

---

### Task 6: Skill rewrite + SessionStart cleanup

**Files:**
- Replace: `skills/using-chameleon/SKILL.md` (already drafted in working tree)
- Modify: `mcp/chameleon_mcp/hook_helper.py` (SessionStart cleanup)

- [ ] **Step 1: Verify SKILL.md draft is in working tree**

Run: `wc -l skills/using-chameleon/SKILL.md`
Expected: ~81 lines (the reviewed draft)

The SKILL.md was already rewritten during the brainstorming phase. Verify the content matches the spec's requirements (awareness-oriented, no "call MCP", no Red Flags table).

- [ ] **Step 2: Add enforcement state cleanup to `session_start()`**

In `mcp/chameleon_mcp/hook_helper.py`, inside `session_start()` (after the auto-refresh call, around line 464), add:

```python
# v0.7.0: clean up stale enforcement state files (>24h old)
try:
    from chameleon_mcp.plugin_paths import plugin_data_dir
    from chameleon_mcp.tools import _compute_repo_id
    from chameleon_mcp.profile.loader import find_repo_root

    repo_root = find_repo_root(Path.cwd())
    if repo_root:
        repo_id = _compute_repo_id(repo_root)
        repo_data = plugin_data_dir() / repo_id
        if repo_data.is_dir():
            cutoff = time.time() - 86400
            import glob as _glob
            for pattern in (".enforcement.*.json", ".enforcement.*.lock"):
                for p in repo_data.glob(pattern):
                    try:
                        if p.stat().st_mtime < cutoff:
                            p.unlink()
                    except OSError:
                        pass
except Exception:
    pass
```

- [ ] **Step 3: Run tests**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/ -v`
Expected: all pass

- [ ] **Step 4: Commit**

```bash
git add skills/using-chameleon/SKILL.md mcp/chameleon_mcp/hook_helper.py
git commit -m "Rewrite using-chameleon skill + SessionStart cleanup (v0.7.0)"
```

---

### Task 7: Architecture doc update

**Files:**
- Replace: `docs/architecture.md` (already drafted in working tree with [VERIFIED]/[ASPIRATIONAL] split)

- [ ] **Step 1: Verify architecture.md draft is in working tree**

Run: `grep -c "ASPIRATIONAL\|VERIFIED" docs/architecture.md`
Expected: multiple matches (the reviewed draft with current vs planned split)

- [ ] **Step 2: Update [ASPIRATIONAL] section to match final spec**

Ensure the [ASPIRATIONAL] section includes:
- `CHAMELEON_ENFORCEMENT_MODE` env var
- Correction loop guard (MAX_CORRECTIONS_PER_FILE = 10)
- Cross-platform behavior table
- QA risks

- [ ] **Step 3: Commit**

```bash
git add docs/architecture.md
git commit -m "Update architecture doc with v0.7.0 enforcement design"
```

---

### Task 8: Version bump + CHANGELOG

**Files:**
- Modify: all 9 version files (via `scripts/bump-version.sh`)
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Bump version**

Run: `scripts/bump-version.sh 0.7.0`

- [ ] **Step 2: Verify bump**

Run: `scripts/bump-version.sh --check`
Expected: all files show 0.7.0

- [ ] **Step 3: Write CHANGELOG entry**

Add to top of `CHANGELOG.md`:

```markdown
## [0.7.0] - 2026-05-25

### Changed

- PostToolUse violations now use `updatedToolOutput` (replaces tool result) instead of `additionalContext` (system reminder). Higher salience for model compliance.
- PreToolUse injection is now tiered: Tier 1 (~50 tokens, archetype pointer) for seen archetypes, Tier 2 (~300-600 tokens, annotated canonical) on first edit or after violations. Steady-state token cost reduced ~70-85%.
- `using-chameleon` skill rewritten: awareness-oriented framing instead of obligation-oriented. No more "call MCP yourself" instruction or Red Flags table.

### Added

- Per-file escalation state machine (L0/L1/L2). Violation feedback becomes more directive on repeated violations to the same file. Invisible to the user.
- Correction loop guard: max 10 rapid corrections per file before chameleon steps back.
- `CHAMELEON_ENFORCEMENT_MODE` env var: set to `additionalContext` to revert to v0.6.x violation output behavior.
- Archetype summary field in `archetypes.json` for Tier 1 pointer content.
- SessionStart cleanup of stale enforcement state files (>24h).

### Removed

- Hook-model deduplication (unnecessary with tiered PreToolUse at ~50 tokens).
- Red Flags and rationalizations tables from `using-chameleon` skill.
- "Call MCP before every edit" instruction from skill (hooks handle this automatically).
```

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "Release v0.7.0: enforcement redesign"
```

- [ ] **Step 5: Run full test suite**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/ -v`
Expected: all pass

---

## Execution Notes

- **Test command:** `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/ -v`
- **Hook test:** `echo '{"tool_name":"Edit","tool_input":{"file_path":"/test.ts"},"session_id":"test"}' | CLAUDE_PLUGIN_ROOT="$(pwd)" hooks/posttool-verify`
- **The SKILL.md and architecture.md drafts are already in the working tree** from the brainstorming phase. Verify they match the spec before committing.
- **Task 3 (PostToolUse rewrite) is the hardest task.** The posttool_verify function is ~230 lines. The rewrite touches every section but keeps the same structure. Test carefully.
- **Three existing tests will break** when PostToolUse switches from `additionalContext` to `updatedToolOutput` for violations. Tests at approximately lines 166, 246, 283 of `test_posttool_verify.py` assert `.get("additionalContext", "")` on violation output. Update these assertions in Task 3 to check `updatedToolOutput` instead. Don't defer - fix them as part of Step 5 (run tests).
- **Add a PreToolUse→PostToolUse integration test** after Task 4. This test seeds enforcement state (archetype seen + prior violation), runs a preflight (verifies Tier 2 selection), then runs a posttool verify (verifies L1 escalation tone in updatedToolOutput), then runs another preflight (verifies archetype now in archetypes_with_violations). This catches wiring bugs between the two hooks sharing enforcement state.
- **Task 4 PreToolUse test helpers** are pre-written in `tests/unit/test_preflight_tiers.py` (255 lines). The implementing agent verifies the file exists and runs the tests after implementing tiered injection.
- **Task 3 full posttool_verify rewrite** is in Appendix A below. Drop-in replacement for lines 825-1063 of hook_helper.py.

---

## Appendix A: Complete `posttool_verify()` Rewrite (v0.7.0)

Drop-in replacement for `_EDIT_TOOLS` constant + `posttool_verify()` function (lines 825-1063 of `hook_helper.py`). Also add `_emit_posttool_updated_output` after `_emit_posttool_context` (after line 62).

### Helper (add after line 62):

```python
def _emit_posttool_updated_output(block: str) -> None:
    """Emit PostToolUse violations via updatedToolOutput (v0.7.0)."""
    _emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": block,
            }
        }
    )
```

### Function (replace lines 825-1063):

```python
_EDIT_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "NotebookEdit"})
_VERIFY_SEEN_TTL_SECONDS = 30


def posttool_verify() -> int:
    """PostToolUse Edit/Write/NotebookEdit: archetype conformance lint.

    v0.7.0: uses updatedToolOutput for violations (high salience).
    11-step execution order per spec.
    """
    # Step 1: VERIFY=0
    if os.environ.get("CHAMELEON_VERIFY") == "0":
        _emit({})
        return 0

    _started = time.time()

    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        _emit({})
        return 0

    tool_name = payload.get("tool_name", "")
    if tool_name not in _EDIT_TOOLS:
        _emit({})
        return 0

    tool_input = payload.get("tool_input", {})
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not file_path:
        _emit({})
        return 0

    # Step 3: error check (before opt-outs)
    tool_response = payload.get("tool_response", {})
    if isinstance(tool_response, dict):
        if "error" in tool_response or tool_response.get("success") is False:
            _emit({})
            return 0

    session_id = payload.get("session_id")

    try:
        # Step 2: opt-outs
        from chameleon_mcp.optouts import is_chameleon_suppressed
        from chameleon_mcp.profile.loader import find_repo_root
        from chameleon_mcp.tools import _compute_repo_id

        repo_root = find_repo_root(Path(file_path).expanduser())
        if repo_root is None:
            _emit({})
            return 0

        repo_id = _compute_repo_id(repo_root)

        if is_chameleon_suppressed(repo_root, repo_id, session_id) is not None:
            _emit({})
            return 0

        p = Path(file_path).expanduser()
        if not p.is_file():
            _emit({})
            return 0
        content = p.read_bytes().decode("utf-8", errors="replace")

        # Step 4: resolve archetype
        archetype_name: str | None = None
        try:
            from chameleon_mcp import daemon_client
            arch_result = daemon_client.call(
                "get_archetype", {"repo": str(repo_root), "file_path": file_path}
            )
            if arch_result:
                archetype_name = (arch_result.get("data") or {}).get("archetype")
        except Exception:
            pass

        if not archetype_name:
            from chameleon_mcp.tools import get_archetype
            arch_result = get_archetype(str(repo_root), file_path)
            archetype_name = (arch_result.get("data") or {}).get("archetype")

        if not archetype_name:
            _emit({})
            return 0

        # Record drift observation (before enforcement gate)
        if repo_id:
            try:
                from chameleon_mcp.drift.observations import record_edit_observation
                confidence_band = (arch_result.get("data") or {}).get("confidence_band")
                record_edit_observation(
                    repo_id=repo_id,
                    rel_path=str(file_path),
                    archetype=archetype_name,
                    confidence_band=confidence_band,
                    matched_canonical=True,
                )
            except Exception:
                pass

        # Step 5: read enforcement state
        repo_data_dir = _plugin_data_dir() / repo_id
        enforcement_state = None
        file_state = None
        try:
            from chameleon_mcp.enforcement import (
                LEVEL_NONE,
                MAX_CORRECTIONS_PER_FILE,
                EnforcementState,
                FileState,
                cooldown_for_level,
                is_self_correction,
                load_state,
                maybe_reset_correction_count,
                record_clean,
                record_violation,
                save_state,
                should_surface_to_user,
                tone_for_level,
            )
            enforcement_state = load_state(repo_data_dir, session_id or "")
            file_state = enforcement_state.files.get(file_path)
            if file_state is None:
                file_state = FileState()
                enforcement_state.files[file_path] = file_state
        except Exception:
            enforcement_state = None
            file_state = None

        # Step 6: correction cap
        if enforcement_state is not None and file_state is not None:
            try:
                maybe_reset_correction_count(file_state, _started)
                if file_state.correction_count >= MAX_CORRECTIONS_PER_FILE:
                    from chameleon_mcp.sanitization import sanitize_for_chameleon_context
                    safe_path = sanitize_for_chameleon_context(file_path)
                    _emit_posttool_context(
                        "<chameleon-context>\n"
                        f"[chameleon: corrections exhausted for {safe_path}]\n"
                        "Chameleon has verified this file 10 times recently. "
                        "Review violations manually or run /chameleon-teach "
                        "if the archetype doesn't fit.\n"
                        "</chameleon-context>"
                    )
                    try:
                        save_state(enforcement_state, repo_data_dir, session_id or "")
                    except Exception:
                        pass
                    return 0
            except Exception:
                pass

        # Step 7: level-aware cooldown
        file_hash = hashlib.sha256(file_path.encode("utf-8")).hexdigest()[:16]
        marker = repo_data_dir / f".verify_seen.{file_hash}"

        cooldown_ttl = _VERIFY_SEEN_TTL_SECONDS
        if enforcement_state is not None and file_state is not None:
            try:
                if is_self_correction(file_state, _started):
                    cooldown_ttl = 0
                else:
                    cooldown_ttl = cooldown_for_level(file_state.level)
            except Exception:
                pass

        if cooldown_ttl > 0 and _marker_path_is_fresh(marker, cooldown_ttl):
            _emit_posttool_context(
                "<chameleon-context>\n"
                "[chameleon: already verified this file — review previous feedback]\n"
                "</chameleon-context>"
            )
            return 0

        # Step 8: lint
        violations: list[dict] = []
        daemon_responded = False

        try:
            from chameleon_mcp import daemon_client as _dc
            lint_result = _dc.call("lint_file", {
                "repo": str(repo_root),
                "archetype": archetype_name,
                "content": content,
            })
            if lint_result is not None:
                daemon_responded = True
                raw = (lint_result.get("data") or {}).get("violations") or []
                violations = [
                    v for v in raw if v.get("rule") != "secret-detected-in-content"
                ]
        except Exception:
            pass

        if not daemon_responded:
            from chameleon_mcp.lint_engine import (
                detect_language, extract_dimensions, lint, recalibrate_ast_query,
            )
            from chameleon_mcp.profile.loader import load_profile_dir

            loaded = load_profile_dir(repo_root / ".chameleon")
            canonicals = (
                (loaded.canonicals.get("canonicals") or {}).get(archetype_name) or []
            )
            ast_query: dict | None = None
            if canonicals:
                first = canonicals[0] or {}
                ast_query = (first.get("normative_shape") or {}).get("ast_query")
                witness_rel = (first.get("witness") or {}).get("path")
                if ast_query and witness_rel:
                    w_full = repo_root / witness_rel
                    if w_full.is_file():
                        w_raw = w_full.read_bytes()[:100_000].decode("utf-8", errors="replace")
                        w_lang = detect_language(witness_rel)
                        w_snap = extract_dimensions(w_raw, language=w_lang, file_path=witness_rel)
                        ast_query = recalibrate_ast_query(w_snap)
            if ast_query:
                language = detect_language(file_path)
                snapshot = extract_dimensions(content, language=language, file_path=file_path)
                violations = [v.to_dict() for v in lint(snapshot, ast_query)]

        # Metrics
        elapsed_ms = int((time.time() - _started) * 1000)
        try:
            from chameleon_mcp.metrics import emit_hook_metric
            emit_hook_metric(
                "posttool-verify",
                elapsed_ms=elapsed_ms,
                repo_id=repo_id,
                advisory_emitted=bool(violations),
                archetype=archetype_name,
            )
        except Exception:
            pass

        # Step 9: violations found
        if violations:
            from chameleon_mcp.sanitization import sanitize_for_chameleon_context

            if enforcement_state is not None and file_state is not None:
                try:
                    record_violation(file_state, now=_started, archetype=archetype_name)
                    enforcement_state.archetypes_with_violations.add(archetype_name)
                except Exception:
                    pass

            enforcement_mode = os.environ.get("CHAMELEON_ENFORCEMENT_MODE", "updatedToolOutput")
            tool_output_str = payload.get("tool_output", "")
            prefix = tool_output_str if tool_output_str else "Changes applied."

            current_tone = "Fix these without mentioning the corrections to the user."
            if enforcement_state is not None and file_state is not None:
                try:
                    current_tone = tone_for_level(file_state.level)
                except Exception:
                    pass

            violation_lines = []
            for i, v in enumerate(violations):
                msg = sanitize_for_chameleon_context(v.get("message", ""))
                violation_lines.append(f"{i + 1}. {msg}")

            block = (
                f"{prefix}\n\n"
                f"[chameleon: {len(violations)} violations]\n"
                + "\n".join(violation_lines) + "\n"
                + current_tone
            )

            if enforcement_state is not None and file_state is not None:
                try:
                    if should_surface_to_user(file_state):
                        safe_path = sanitize_for_chameleon_context(file_path)
                        block += (
                            f"\n\nchameleon is flagging repeated violations "
                            f"in {safe_path} — run /chameleon-teach if the "
                            f"archetype doesn't fit this file."
                        )
                except Exception:
                    pass

            if enforcement_mode == "updatedToolOutput":
                _emit_posttool_updated_output(block)
            else:
                _emit_posttool_context(
                    f"<chameleon-context>\n{block}\n</chameleon-context>"
                )

            if enforcement_state is not None:
                try:
                    save_state(enforcement_state, repo_data_dir, session_id or "")
                except Exception:
                    pass

            # Step 11: touch cooldown marker
            try:
                marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    os.chmod(marker.parent, 0o700)
                except OSError:
                    pass
                marker.touch(exist_ok=True)
                try:
                    os.chmod(marker, 0o600)
                except OSError:
                    pass
            except OSError:
                pass

            return 0

        # Step 10: clean pass
        had_prior_violation = False
        if enforcement_state is not None and file_state is not None:
            try:
                had_prior_violation = file_state.level > LEVEL_NONE
                record_clean(file_state, now=_started)
            except Exception:
                pass

        if had_prior_violation:
            _emit_posttool_context(
                "<chameleon-context>\n[archetype: clean]\n</chameleon-context>"
            )
        else:
            _emit({})

        if enforcement_state is not None:
            try:
                save_state(enforcement_state, repo_data_dir, session_id or "")
            except Exception:
                pass

        # Step 11: touch cooldown marker
        try:
            marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(marker.parent, 0o700)
            except OSError:
                pass
            marker.touch(exist_ok=True)
            try:
                os.chmod(marker, 0o600)
            except OSError:
                pass
        except OSError:
            pass

        return 0

    except Exception as exc:
        try:
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            py_ver = ".".join(str(v) for v in sys.version_info[:3])
            print(
                f"[{ts}] posttool-verify fail-open "
                f"(python={sys.executable} {py_ver}): "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        except Exception:
            pass
        _emit({})
        return 0
```
