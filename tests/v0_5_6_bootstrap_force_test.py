"""Regression test for BUG-026: bootstrap_repo guards against accidental overwrite.

Pre-v0.5.6, calling bootstrap_repo twice on the same path silently
overwrote the existing profile. The /chameleon-init skill warned the
model about this but the MCP itself had no guard. Defense in depth:
require an explicit ``force=True`` to overwrite a committed profile.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_6_bootstrap_force_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_6_bootstrap_force_data_")
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


from chameleon_mcp.tools import bootstrap_repo  # noqa: E402


def _setup_repo(td: str) -> Path:
    root = Path(td)
    (root / "package.json").write_text(
        '{"name":"x","dependencies":{"typescript":"5"}}'
    )
    (root / "tsconfig.json").write_text("{}")
    for i in range(4):
        (root / f"foo{i}.ts").write_text(f"export const v{i} = {i};\n")
    return root


def main() -> int:
    print("=== BUG-026: bootstrap_repo refuses to overwrite without force ===")
    with tempfile.TemporaryDirectory(prefix="bug026_") as td:
        root = _setup_repo(td)

        first = bootstrap_repo(str(root))
        t(
            "first bootstrap succeeds",
            first["data"].get("status") == "success",
            f"got {first['data'].get('status')!r}",
        )

        second = bootstrap_repo(str(root))
        t(
            "second bootstrap returns already_bootstrapped (not silent overwrite)",
            second["data"].get("status") == "already_bootstrapped",
            f"got {second['data'].get('status')!r}",
        )
        t(
            "already_bootstrapped envelope carries profile_path",
            bool(second["data"].get("profile_path")),
            f"got {second['data']!r}",
        )

        third = bootstrap_repo(str(root), force=True)
        t(
            "explicit force=True overwrites",
            third["data"].get("status") == "success",
            f"got {third['data'].get('status')!r}",
        )

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
