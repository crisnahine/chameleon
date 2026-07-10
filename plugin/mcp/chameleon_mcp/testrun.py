"""Opt-in repo-local test runner for the auto-pass router.

Running a repo's own test suite is arbitrary code execution (the runner, its
config, and every test it loads are repo-controlled), so this is gated and fails
open, mirroring :func:`chameleon_mcp.typecheck.run_tsc`:

  - Gated behind ``CHAMELEON_ALLOW_TESTS=1``. Without it the caller records the
    test run as unavailable rather than executing repo code behind the user's
    back.
  - The runner resolves ONLY from the repo's own ``node_modules/.bin`` -- never
    PATH, never npx, never a download.
  - Hard wall-clock timeout; a hung suite can never trap the tool call.
  - Three-state result: ``unavailable`` (no signal -- a recorded fact, never a
    failure), ``clean``, or ``failures``. When no repo-local runner is found, or
    the runner exits non-zero WITHOUT having run any tests (a config error), the
    result reads as unavailable, never as a failure or a clean run.

Tool-time only: this module is never imported by hooks or any hook hot path.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from chameleon_mcp._thresholds import threshold_int

ALLOW_ENV = "CHAMELEON_ALLOW_TESTS"

# (runner name, argv after the binary) in preference order. Both run the suite
# once and exit (no watch); the exit code is the pass/fail signal.
_RUNNERS: tuple[tuple[str, list[str]], ...] = (
    ("vitest", ["run", "--reporter=dot"]),
    ("jest", ["--ci", "--silent"]),
)


def is_enabled() -> bool:
    """True when the operator opted into executing the repo's test suite."""
    return os.environ.get(ALLOW_ENV) == "1"


# Substrings (case-folded) that mark a run where the suite actually executed.
# jest prints "Tests:       1 failed, 2 passed"; vitest prints "Test Files ..."
# and "Tests  3 passed | 1 failed". Their absence on a non-zero exit means the
# runner died before running anything (a config / missing-dep error).
_TEST_RAN_MARKERS = ("test files", "tests:", "passed", "failed", "✓", "✗")


def _ran_tests(output: str) -> bool:
    low = output.lower()
    return any(marker in low for marker in _TEST_RAN_MARKERS)


def _unavailable(reason: str) -> dict:
    """A structured "no signal" result: the test run could not run."""
    return {"status": "unavailable", "reason": reason}


def _resolve_runner(repo_root: Path) -> tuple[Path, list[str]] | None:
    """The repo's own jest/vitest binary + argv, or None when none is installed.

    Resolution is deliberately limited to ``<repo>/node_modules/.bin``: a global
    runner on PATH may not match the repo's version, and consulting PATH would
    execute a binary the opt-in never vouched for.
    """
    bin_dir = repo_root / "node_modules" / ".bin"
    for name, args in _RUNNERS:
        exe = bin_dir / (f"{name}.cmd" if os.name == "nt" else name)
        if exe.is_file():
            return exe, args
    return None


def run_tests(repo_root: Path) -> dict:
    """Run the repo's own test runner once and return a three-state result.

    ``{"status": "unavailable", "reason": ...}`` when no repo-local runner is
    installed, on timeout/spawn error, or on a non-zero exit with no sign the
    suite ran (a config error); ``{"status": "clean", "runner": ...}`` on a zero
    exit; ``{"status": "failures", "runner": ..., "exit_code": N}`` when the suite
    ran and exited non-zero. Never raises.
    """
    resolved = _resolve_runner(repo_root)
    if resolved is None:
        return _unavailable("no repo-local vitest/jest in node_modules/.bin")
    exe, args = resolved
    try:
        proc = subprocess.run(
            [str(exe), *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=threshold_int("AUTOPASS_TESTRUN_TIMEOUT_SECONDS"),
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return _unavailable("test runner timed out or could not spawn")

    if proc.returncode == 0:
        return {"status": "clean", "runner": exe.name}
    # A non-zero exit only counts as a test FAILURE when the suite actually ran;
    # otherwise it is a config / missing-dep error, which is "no signal" (the same
    # distinction the tsc runner draws between diagnostics and a config-error exit).
    if not _ran_tests((proc.stdout or "") + "\n" + (proc.stderr or "")):
        return _unavailable(
            f"test runner exited {proc.returncode} with no test output (config error?)"
        )
    return {"status": "failures", "runner": exe.name, "exit_code": proc.returncode}
