"""Regression test for BUG-020: parse eslint.config.mjs flat config.

Pre-v0.5.6 the extractor only looked at .eslintrc.{json,js,cjs,mjs,yml}
files. ESLint 9+'s flat config (eslint.config.{js,mjs,cjs,ts}) was
completely skipped — even mastodon's 10KB eslint.config.mjs produced
zero rules.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_6_eslint_flat_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_6_eslint_flat_data_")
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


from chameleon_mcp.bootstrap.tool_config import _parse_eslint_js, read_tool_configs  # noqa: E402


def main() -> int:
    print("=== BUG-020: eslint.config.mjs (flat config) extraction ===")

    fixture = HERE.parent / "fixtures" / "eslint_flat" / "eslint.config.mjs"
    parsed, warning = _parse_eslint_js(fixture)
    t(
        "parser returns dict (not None)",
        parsed is not None,
        f"warning={warning!r}",
    )
    if parsed is None:
        print(f"\nResults: {PASS} passed, {FAIL} failed")
        return 1
    t("parsed payload flagged as flat", parsed.get("flat") is True)
    t(
        "rules dict carries known entries",
        isinstance(parsed.get("rules"), dict)
        and parsed["rules"].get("prefer-const") == "error",
        f"got rules={parsed.get('rules')!r}",
    )

    # read_tool_configs at the fixture dir should pick up the flat config
    # and surface it under result.eslint with the correct source.
    result = read_tool_configs(HERE.parent / "fixtures" / "eslint_flat")
    t(
        "read_tool_configs records eslint.config.mjs as the source",
        result.sources.get("eslint") == "eslint.config.mjs",
        f"got {result.sources!r}",
    )
    t(
        "read_tool_configs populates result.eslint",
        isinstance(result.eslint, dict) and bool(result.eslint),
        f"got {result.eslint!r}",
    )

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
