"""Per-file enforcement state machine.

Tracks escalation levels (L0/L1/L2), correction counts, and cooldowns
per file per session. State persists in a JSON file under the plugin
data directory, guarded by flock.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from chameleon_mcp.locks import LockHeldError, acquire_advisory_lock

SAVE_LOCK_TIMEOUT_SECONDS = 5.0

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
    blockable_unresolved: bool = False

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "violation_count": self.violation_count,
            "correction_count": self.correction_count,
            "last_violation_at": self.last_violation_at,
            "last_verified_at": self.last_verified_at,
            "last_clean_at": self.last_clean_at,
            "consecutive_l2": self.consecutive_l2,
            "blockable_unresolved": self.blockable_unresolved,
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
            blockable_unresolved=d.get("blockable_unresolved", False),
        )


@dataclass
class EnforcementState:
    archetypes_seen: set[str] = field(default_factory=set)
    archetypes_with_violations: set[str] = field(default_factory=set)
    files: dict[str, FileState] = field(default_factory=dict)
    stop_hook_blocks: int = 0
    duplication_spawns: int = 0
    correctness_spawns: int = 0

    def to_dict(self) -> dict:
        return {
            "archetypes_seen": sorted(self.archetypes_seen),
            "archetypes_with_violations": sorted(self.archetypes_with_violations),
            "files": {k: v.to_dict() for k, v in self.files.items()},
            "stop_hook_blocks": self.stop_hook_blocks,
            "duplication_spawns": self.duplication_spawns,
            "correctness_spawns": self.correctness_spawns,
        }

    @classmethod
    def from_dict(cls, d: dict) -> EnforcementState:
        return cls(
            archetypes_seen=set(d.get("archetypes_seen", [])),
            archetypes_with_violations=set(d.get("archetypes_with_violations", [])),
            files={k: FileState.from_dict(v) for k, v in d.get("files", {}).items()},
            stop_hook_blocks=d.get("stop_hook_blocks", 0),
            duplication_spawns=d.get("duplication_spawns", 0),
            correctness_spawns=d.get("correctness_spawns", 0),
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


def _merge_states(disk: EnforcementState, mem: EnforcementState) -> EnforcementState:
    """Merge an in-memory state with whatever a concurrent writer left on disk.

    The archetype sets are monotonic within a session, so union them. For per
    file entries, keep the most-recently-verified one. This prevents parallel
    agents that share a session_id from clobbering each other's updates.
    """
    merged = EnforcementState(
        archetypes_seen=disk.archetypes_seen | mem.archetypes_seen,
        archetypes_with_violations=(
            disk.archetypes_with_violations | mem.archetypes_with_violations
        ),
        files=dict(disk.files),
    )
    for key, mfs in mem.files.items():
        dfs = merged.files.get(key)
        if dfs is None or (mfs.last_verified_at or 0) >= (dfs.last_verified_at or 0):
            merged.files[key] = mfs
    merged.stop_hook_blocks = max(disk.stop_hook_blocks, mem.stop_hook_blocks)
    merged.duplication_spawns = max(disk.duplication_spawns, mem.duplication_spawns)
    merged.correctness_spawns = max(disk.correctness_spawns, mem.correctness_spawns)
    return merged


def save_state(
    state: EnforcementState,
    repo_dir: Path,
    session_id: str,
    *,
    prune_missing: bool = False,
) -> None:
    """Persist enforcement state, merging with whatever a concurrent writer left.

    ``prune_missing`` drops file entries whose absolute path no longer exists on
    disk. The merge is otherwise additive (it never removes a disk-only entry, so
    parallel agents sharing a session don't lose each other's files), which means
    a deleted file's entry would otherwise live until eviction. The Stop backstop
    sets this once per turn end to keep state from accumulating phantom paths; the
    per-edit callers leave it off so they don't pay a stat per file on every save.
    """
    path = _state_path(repo_dir, session_id)
    lock = _lock_path(repo_dir, session_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass

    def _merge_and_write() -> None:
        merged = _merge_states(load_state(repo_dir, session_id), state)
        if prune_missing:
            for fpath in list(merged.files):
                try:
                    if not Path(fpath).is_file():
                        del merged.files[fpath]
                except OSError:
                    pass
        _evict_if_needed(merged)
        # Per-write tmp name so two writers never collide on the same tmp file,
        # even on the degraded path below where the lock could not be held.
        tmp = path.with_suffix(f".{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(json.dumps(merged.to_dict(), separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        # Reap tmp files orphaned by a writer killed mid-write. Safe under the
        # lock: no other writer for this session is producing a tmp right now.
        for orphan in path.parent.glob(f"{path.stem}.*.tmp"):
            try:
                orphan.unlink()
            except OSError:
                pass

    # Hold the lock across the whole load+merge+write so concurrent writers that
    # share a session_id serialize their read-modify-write and cannot lose each
    # other's entries. A contended save blocks-and-retries rather than falling
    # back to an unlocked write (which produced lost updates). The lock is broken
    # only when its holder is dead or older than the stale ceiling.
    try:
        with acquire_advisory_lock(
            lock,
            stale_after_seconds=60,
            blocking_timeout=SAVE_LOCK_TIMEOUT_SECONDS,
        ):
            _merge_and_write()
    except (LockHeldError, OSError):
        # The lock could not be held (a live holder kept it past the blocking
        # window, or the lock file itself was unwritable). Skipping is safer
        # than racing an unlocked write, which clobbered a concurrent writer's
        # data. Existing on-disk state is preserved; this session's update lands
        # on the next edit that does acquire the lock.
        pass


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


def maybe_reset_correction_count(fs: FileState, now: float) -> None:
    """Zero correction_count if the 60s reset window has elapsed."""
    if correction_count_reset(fs, now):
        fs.correction_count = 0


def tone_for_level(level: int) -> str:
    if level <= LEVEL_L0:
        return "Fix these."
    if level == LEVEL_L1:
        return "Fix these. This file was flagged before."
    return "STOP. Fix these violations before any other edit."


def record_violation(
    fs: FileState,
    *,
    now: float,
    archetype: str,
    hard_class: bool = False,
) -> None:
    fs.violation_count += 1
    fs.correction_count += 1
    self_corr = is_self_correction(fs, now)
    fs.last_violation_at = now
    fs.last_verified_at = now
    if hard_class:
        fs.blockable_unresolved = True

    if fs.level == LEVEL_NONE:
        fs.level = LEVEL_L0
    elif not self_corr:
        if fs.level < LEVEL_L2:
            fs.level += 1
        if fs.level == LEVEL_L2:
            fs.consecutive_l2 += 1


def record_clean(fs: FileState, *, now: float) -> None:
    fs.blockable_unresolved = False
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
