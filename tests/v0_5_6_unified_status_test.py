"""Regression test for BUG-012: unify "no source files" status.

Pre-v0.5.6:
- A workspace with TS deps but ZERO .ts files returned status='failed'.
- A workspace with no language signals at all returned
  status='failed_unsupported_language'.
Both are semantically the same case ("nothing for chameleon to do");
callers had to track two distinct statuses for no real reason.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_6_unified_status_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_6_unified_data_")
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


def main() -> int:
    print("=== BUG-012: unify failed_unsupported_language status ===")

    # Case A: TS deps + tsconfig but ZERO .ts files
    with tempfile.TemporaryDirectory(prefix="bug012_no_source_") as td:
        root = Path(td)
        (root / "package.json").write_text(
            '{"name":"x","dependencies":{"typescript":"5"}}'
        )
        (root / "tsconfig.json").write_text("{}")
        # Intentionally NO .ts/.tsx files
        (root / "README.md").write_text("# nothing here")
        resp = bootstrap_repo(str(root))
        status = resp["data"].get("status")
        t(
            "no-source workspace returns failed_unsupported_language (was 'failed')",
            status == "failed_unsupported_language",
            f"got {status!r}",
        )

    # Case B: no language signals at all — should also be failed_unsupported_language
    with tempfile.TemporaryDirectory(prefix="bug012_no_signals_") as td:
        root = Path(td)
        (root / "README.md").write_text("# python")
        resp2 = bootstrap_repo(str(root))
        status2 = resp2["data"].get("status")
        t(
            "no-signals workspace also returns failed_unsupported_language",
            status2 == "failed_unsupported_language",
            f"got {status2!r}",
        )

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
