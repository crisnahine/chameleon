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
        # A daemon spawned in one act outlives it (default idle timeout 600s)
        # and can hold profile locks while later acts contend for them. A short
        # idle window keeps cross-act daemon lifetime from amplifying any lock
        # contention into a multi-act stall.
        "CHAMELEON_DAEMON_IDLE_TIMEOUT": "60",
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
