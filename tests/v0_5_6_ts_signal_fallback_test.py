"""Regression test for BUG-010: TS detection fallback to *.ts file presence.

Pre-v0.5.6, a workspace with package.json lacking TS deps and no own
tsconfig.json (hoisted-deps monorepo, e.g. excalidraw-app) was reported
as failed_unsupported_language even though it contained .ts/.tsx files.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_6_ts_signal_fallback_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_6_ts_signal_data_")
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


from chameleon_mcp.extractors.typescript import (  # noqa: E402
    TypeScriptExtractor,
    _has_typescript_source_files,
)


def main() -> int:
    print("=== BUG-010: TS detection accepts .ts file presence ===")

    with tempfile.TemporaryDirectory(prefix="bug010_") as td:
        root = Path(td)
        # Hoisted-deps style: workspace package.json with NO ts deps,
        # NO own tsconfig, but a real src/ dir with .ts files.
        (root / "package.json").write_text(
            '{"name":"excalidraw-app","private":true,"scripts":{"build":"vite build"}}'
        )
        src = root / "src"
        src.mkdir()
        (src / "App.tsx").write_text("export const App = () => null;\n")
        (src / "main.ts").write_text("export const x = 1;\n")

        # Older signal check would have returned False
        # (vite is in scripts not deps, no tsconfig, no ts deps).
        # Helper check:
        t(
            "_has_typescript_source_files finds the .ts files",
            _has_typescript_source_files(root),
            "should detect .ts/.tsx within depth 3",
        )

        ext = TypeScriptExtractor()
        t(
            "TypeScriptExtractor.can_handle accepts this workspace",
            ext.can_handle(root),
            "fallback signal should pass",
        )

    # Negative case: directory with no .ts files
    with tempfile.TemporaryDirectory(prefix="bug010_neg_") as td:
        root = Path(td)
        (root / "package.json").write_text('{"name":"y"}')
        (root / "main.py").write_text("print('hi')\n")
        ext = TypeScriptExtractor()
        t(
            "no .ts files → can_handle returns False",
            not ext.can_handle(root),
        )

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
