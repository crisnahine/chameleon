"""Scenario dataclasses and Context for the chameleon dogfood harness."""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Callable, Literal

CostBand = Literal["free", "cheap", "moderate", "expensive"]
StatusName = Literal["PASS", "FAIL", "SKIP", "ERROR"]


@dataclasses.dataclass
class Context:
    """Shared per-run context passed to each scenario."""
    plugin_root: Path           # absolute path to chameleon repo
    plugin_data_dir: Path       # ephemeral; per-scenario tmpdir, set by runner
    repo_paths: dict[str, Path] # keys: "ts", "ruby" (per-env-var or None)
    real_claude_allowed: bool
    cost_so_far_usd: float


@dataclasses.dataclass
class Result:
    status: StatusName
    notes: str = ""
    cost_usd: float = 0.0
    duration_s: float = 0.0


@dataclasses.dataclass
class Scenario:
    id: str                            # e.g. "1.1"
    name: str                          # e.g. "/chameleon-init cooperative"
    family: str                        # e.g. "init"
    needs_claude: bool                 # True if requires real claude -p
    cost: CostBand                     # free | cheap | moderate | expensive
    requires: list[str] = dataclasses.field(default_factory=list)
    # Optional setup / teardown; defaults are no-op
    setup: Callable[[Context], None] | None = None
    teardown: Callable[[Context], None] | None = None
    # The actual test
    run: Callable[[Context], Result] | None = None

    def is_runnable(self, ctx: Context) -> tuple[bool, str]:
        """Return (True, '') or (False, 'skip reason')."""
        for env in self.requires:
            if env.startswith("env:"):
                varname = env[4:]
                if not __import__("os").environ.get(varname):
                    return False, f"missing env {varname}"
            elif env.startswith("repo:"):
                key = env[5:]
                if ctx.repo_paths.get(key) is None or not ctx.repo_paths[key].is_dir():
                    return False, f"missing repo {key}"
            elif env.startswith("fixture:"):
                fp = ctx.plugin_root / env[8:]
                if not fp.is_dir():
                    return False, f"missing fixture {env[8:]}"
        if self.needs_claude and not ctx.real_claude_allowed:
            return False, "needs --include-real-claude"
        return True, ""
