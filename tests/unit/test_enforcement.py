"""Unit tests for enforcement state machine."""

from __future__ import annotations

import time
from pathlib import Path


def _make_data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "chameleon_data" / "test_repo_id"
    d.mkdir(parents=True)
    return d


def test_empty_state():
    from chameleon_mcp.enforcement import EnforcementState

    state = EnforcementState()
    assert state.archetypes_seen == set()
    assert state.archetypes_with_violations == set()
    assert state.files == {}


def test_file_state_defaults():
    from chameleon_mcp.enforcement import FileState

    fs = FileState()
    assert fs.level == -1
    assert fs.violation_count == 0
    assert fs.correction_count == 0
    assert fs.last_violation_at is None
    assert fs.last_verified_at is None
    assert fs.last_clean_at is None
    assert fs.consecutive_l2 == 0


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


def test_save_merges_concurrent_on_disk_state(tmp_path):
    """A stale in-memory save must not clobber a concurrent writer's updates."""
    from chameleon_mcp.enforcement import (
        EnforcementState,
        FileState,
        load_state,
        save_state,
    )

    repo_dir = _make_data_dir(tmp_path)

    # Agent A persists its view.
    state_a = EnforcementState(
        archetypes_seen={"alpha"},
        files={"/a.ts": FileState(level=1, last_verified_at=100.0)},
    )
    save_state(state_a, repo_dir, "shared-session")

    # Agent B loaded before A's save (so it doesn't know about alpha/a.ts) and
    # now persists its own view.
    state_b = EnforcementState(
        archetypes_seen={"beta"},
        files={"/b.ts": FileState(level=2, last_verified_at=200.0)},
    )
    save_state(state_b, repo_dir, "shared-session")

    merged = load_state(repo_dir, "shared-session")
    assert merged.archetypes_seen == {"alpha", "beta"}
    assert set(merged.files) == {"/a.ts", "/b.ts"}
    assert merged.files["/b.ts"].level == 2


def test_load_missing_returns_empty(tmp_path):
    from chameleon_mcp.enforcement import load_state

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


def test_eviction_at_200_files(tmp_path):
    from chameleon_mcp.enforcement import (
        EnforcementState,
        FileState,
        load_state,
        save_state,
    )

    repo_dir = _make_data_dir(tmp_path)
    state = EnforcementState()
    now = time.time()
    for i in range(210):
        state.files[f"/file_{i:04d}.ts"] = FileState(
            level=0,
            last_verified_at=now - (210 - i),
        )
    save_state(state, repo_dir, "session-evict")
    loaded = load_state(repo_dir, "session-evict")
    assert len(loaded.files) == 200
    assert "/file_0000.ts" not in loaded.files
    assert "/file_0209.ts" in loaded.files


def test_record_violation_no_state_to_l0():
    from chameleon_mcp.enforcement import LEVEL_L0, FileState, record_violation

    fs = FileState()
    now = time.time()
    record_violation(fs, now=now, archetype="component")
    assert fs.level == LEVEL_L0
    assert fs.violation_count == 1
    assert fs.correction_count == 1
    assert fs.last_violation_at == now


def test_record_violation_l0_to_l1_different_edit():
    from chameleon_mcp.enforcement import LEVEL_L0, LEVEL_L1, FileState, record_violation

    fs = FileState(level=LEVEL_L0, last_violation_at=time.time() - 20)
    now = time.time()
    record_violation(fs, now=now, archetype="component")
    assert fs.level == LEVEL_L1


def test_record_violation_self_correction_no_escalation():
    from chameleon_mcp.enforcement import LEVEL_L0, FileState, record_violation

    first = time.time()
    fs = FileState(level=LEVEL_L0, last_violation_at=first)
    now = first + 5
    record_violation(fs, now=now, archetype="component")
    assert fs.level == LEVEL_L0
    assert fs.correction_count == 1


def test_record_violation_l1_to_l2():
    from chameleon_mcp.enforcement import LEVEL_L1, LEVEL_L2, FileState, record_violation

    fs = FileState(level=LEVEL_L1, last_violation_at=time.time() - 20)
    record_violation(fs, now=time.time(), archetype="component")
    assert fs.level == LEVEL_L2


def test_consecutive_l2_increments():
    from chameleon_mcp.enforcement import LEVEL_L2, FileState, record_violation

    fs = FileState(level=LEVEL_L2, last_violation_at=time.time() - 20, consecutive_l2=1)
    record_violation(fs, now=time.time(), archetype="component")
    assert fs.consecutive_l2 == 2


def test_record_clean_de_escalates():
    from chameleon_mcp.enforcement import LEVEL_L1, LEVEL_L2, FileState, record_clean

    fs = FileState(level=LEVEL_L2, correction_count=3, consecutive_l2=2)
    record_clean(fs, now=time.time())
    assert fs.level == LEVEL_L1
    assert fs.correction_count == 0
    assert fs.consecutive_l2 == 0


def test_record_clean_l0_to_none():
    from chameleon_mcp.enforcement import LEVEL_L0, LEVEL_NONE, FileState, record_clean

    fs = FileState(level=LEVEL_L0)
    record_clean(fs, now=time.time())
    assert fs.level == LEVEL_NONE


def test_should_surface_to_user_at_3_consecutive_l2():
    from chameleon_mcp.enforcement import FileState, should_surface_to_user

    fs = FileState(consecutive_l2=3)
    assert should_surface_to_user(fs) is True


def test_should_surface_fast_path_no_clean_ever():
    from chameleon_mcp.enforcement import (
        LEVEL_L2,
        FileState,
        should_surface_to_user,
    )

    fs = FileState(level=LEVEL_L2, consecutive_l2=1, last_clean_at=None)
    assert should_surface_to_user(fs) is True


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
    assert is_self_correction(fs, now) is True

    fs2 = FileState(last_violation_at=now - 10.001)
    assert is_self_correction(fs2, now) is False


def test_eviction_with_none_last_verified_at(tmp_path):
    from chameleon_mcp.enforcement import (
        EnforcementState,
        FileState,
        load_state,
        save_state,
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
    assert "/none_file.ts" not in loaded.files
