"""Opt-in repo-local ``tsc --noEmit`` runner for the auto-pass router.

Running a repo's own TypeScript compiler is arbitrary code execution (the
binary, its config, and every plugin it loads are repo-controlled), so this is
gated and fails open:

  - Gated behind ``CHAMELEON_ALLOW_TSC=1``. Without it the caller records the
    typecheck as unavailable rather than executing repo code behind the user's
    back.
  - ``tsc`` resolves ONLY from the repo's own ``node_modules/.bin`` -- never
    PATH, never npx, never a download. The binary being the repo's own
    dependency is exactly why execution is opt-in.
  - Hard wall-clock timeout; a hung compile can never trap the tool call.
  - Three-state result: ``unavailable`` (no signal -- a recorded fact, never a
    failure), ``clean``, or ``errors`` with the affected files. A compiler that
    exits non-zero without parseable diagnostics (a config error) reads as
    unavailable, never as a clean run.

Tool-time only: this module is never imported by hooks or any hook hot path.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from chameleon_mcp._thresholds import threshold_int
from chameleon_mcp.grounding import files_with_type_errors, parse_tsc_output

ALLOW_ENV = "CHAMELEON_ALLOW_TSC"


def is_enabled() -> bool:
    """True when the operator opted into executing the repo's tsc binary."""
    return os.environ.get(ALLOW_ENV) == "1"


def _unavailable(reason: str) -> dict:
    """A structured "no signal" result: the typecheck could not run."""
    return {"status": "unavailable", "reason": reason}


def _tsc_binary(repo_root: Path) -> Path | None:
    """The repo's own tsc binary, or None when it is not installed.

    Resolution is deliberately limited to ``<repo>/node_modules/.bin``: a
    global tsc on PATH may not match the repo's TypeScript version, and
    consulting PATH would execute a binary the opt-in never vouched for.
    """
    name = "tsc.cmd" if os.name == "nt" else "tsc"
    candidate = repo_root / "node_modules" / ".bin" / name
    return candidate if candidate.is_file() else None


def run_tsc(repo_root: Path) -> dict:
    """Run ``tsc --noEmit`` in ``repo_root`` and return a three-state result.

    ``{"status": "unavailable", "reason": ...}`` when the check could not run
    (no root tsconfig.json, no repo-local binary, timeout, or a config-error
    exit with no diagnostics); ``{"status": "clean", "files": []}`` on a clean
    compile; ``{"status": "errors", "files": [...], "diagnostics": N}`` with
    repo-relative POSIX paths otherwise. Never raises.
    """
    if not (repo_root / "tsconfig.json").is_file():
        return _unavailable("no tsconfig.json at repo root")
    binary = _tsc_binary(repo_root)
    if binary is None:
        return _unavailable("tsc not installed in repo node_modules")
    try:
        proc = subprocess.run(
            [str(binary), "--noEmit", "--pretty", "false"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=threshold_int("AUTOPASS_TSC_TIMEOUT_SECONDS"),
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return _unavailable("tsc timed out or could not spawn")

    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    rows = parse_tsc_output(output)
    if not rows:
        if proc.returncode == 0:
            return {"status": "clean", "files": []}
        # No diagnostics but a failing exit: the compiler never got to checking
        # (bad tsconfig, missing inputs). That is "no signal", not "clean".
        return _unavailable(
            f"tsc exited {proc.returncode} with no parseable diagnostics (config error?)"
        )
    # Forward slashes so the paths compare equal to git's repo-relative output.
    files = sorted(f.replace("\\", "/") for f in files_with_type_errors(output))
    return {"status": "errors", "files": files, "diagnostics": len(rows)}
