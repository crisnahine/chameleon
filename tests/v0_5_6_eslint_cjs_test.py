"""Regression test for BUG-003: .eslintrc.cjs parser works via node.

Pre-v0.5.6, .eslintrc.cjs failed with "object literal not JSON-coercible"
because the regex-based parser couldn't handle real-world configs with
nested objects, parserOptions, etc. Now we shell out to Node which gives
the same value ESLint sees.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_6_eslint_cjs_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_6_eslintcjs_data_")
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


from chameleon_mcp.bootstrap.tool_config import _parse_eslint_js  # noqa: E402


def main() -> int:
    print("=== BUG-003: .eslintrc.cjs parsed via node ===")

    fixture = HERE.parent / "fixtures" / "eslint_cjs" / ".eslintrc.cjs"
    parsed, warning = _parse_eslint_js(fixture)

    t(
        "parser returns dict (not None)",
        parsed is not None,
        f"warning={warning!r}",
    )
    if parsed is None:
        print(f"\nResults: {PASS} passed, {FAIL} failed")
        return 1

    t("captures root: true", parsed.get("root") is True, f"got {parsed.get('root')!r}")
    t(
        "captures rules dict",
        isinstance(parsed.get("rules"), dict)
        and parsed["rules"].get("no-console") == "warn",
        f"got rules={parsed.get('rules')!r}",
    )
    t(
        "captures plugins list",
        isinstance(parsed.get("plugins"), list)
        and "check-file" in parsed["plugins"],
        f"got plugins={parsed.get('plugins')!r}",
    )

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
