"""Regression test for BUG-007: ENGINE_MIN_VERSION reads from package metadata.

Pre-v0.5.6, ENGINE_MIN_VERSION was hardcoded to "0.4.0", which leaked
into profile.json and profile.summary.md forever. Now it reads from
the installed package metadata via importlib.metadata.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_6_engine_version_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_6_engine_version_data_")
os.environ["CHAMELEON_PLUGIN_DATA"] = TMPDATA

PASS = 0
FAIL = 0


def t(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("=== BUG-007: ENGINE_MIN_VERSION reads from package metadata ===")
    from chameleon_mcp.bootstrap.orchestrator import ENGINE_MIN_VERSION

    # Read pyproject.toml's project.version
    try:
        import tomllib  # 3.11+
    except ImportError:
        import tomli as tomllib  # type: ignore

    pyproject = HERE.parent.parent / "mcp" / "pyproject.toml"
    with open(pyproject, "rb") as fh:
        data = tomllib.load(fh)
    pkg_version = data["project"]["version"]

    t(
        f"ENGINE_MIN_VERSION ({ENGINE_MIN_VERSION!r}) matches pyproject ({pkg_version!r})",
        ENGINE_MIN_VERSION == pkg_version,
    )

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
