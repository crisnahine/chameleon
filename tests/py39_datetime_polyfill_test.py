"""Pin the rec-4 (v0.5.14 bug 4) fix: `datetime.UTC` polyfill for Py<3.11.

The bug: the chameleon hook-side bash wrapper falls back to system
python3 when the plugin lacks a bundled venv. On macOS Command Line
Tools that's Python 3.9, where `datetime.UTC` does not exist. Code that
did `from datetime import UTC` raised ImportError, the hook crashed,
and recent_hook_errors in /chameleon-doctor showed:
  "drift banner failed: ImportError: cannot import name 'UTC' from 'datetime'"

The fix: import UTC inside a try/except that falls back to
`datetime.timezone.utc` (available since Py3.2) on ImportError.

These tests simulate Py<3.11 by reloading the module with `UTC`
hidden from the datetime namespace, then assert the module still
imports and UTC is the expected sentinel value.
"""

from __future__ import annotations

import datetime as _datetime
import importlib
import inspect
import sys
from unittest.mock import patch

PASS: list[tuple[str, str]] = []
FAIL: list[tuple[str, str]] = []


def t(name: str, condition: bool, info: str = "") -> None:
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' - ' + info) if info else ''}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def _reload_under_simulated_py39(module_name: str):
    """Re-import `module_name` with `datetime.UTC` shadowed away.

    Wraps the datetime module so attribute lookup of `UTC` raises
    ImportError, mimicking Py3.9 where UTC simply doesn't exist.
    """
    real_dt = _datetime
    for m in list(sys.modules):
        if m == module_name or m.startswith(f"{module_name}."):
            sys.modules.pop(m, None)

    class _DatetimeWithoutUTC:
        __name__ = "datetime"

        def __getattr__(self, name):
            if name == "UTC":
                raise ImportError(
                    "cannot import name 'UTC' from 'datetime' (simulated Py3.9)"
                )
            return getattr(real_dt, name)

    fake = _DatetimeWithoutUTC()
    with patch.dict(sys.modules, {"datetime": fake}):
        return importlib.import_module(module_name)


section("optouts module imports cleanly under simulated Py<3.11")
mod = _reload_under_simulated_py39("chameleon_mcp.optouts")
t("optouts imported without ImportError", mod is not None)
t(
    "optouts.UTC is the timezone.utc fallback",
    mod.UTC is _datetime.UTC,
)


section("optouts module still works on real interpreter")
for m in list(sys.modules):
    if m == "chameleon_mcp.optouts" or m.startswith("chameleon_mcp.optouts."):
        sys.modules.pop(m, None)
import chameleon_mcp.optouts as real_optouts  # noqa: E402

t(
    "optouts.UTC is a tzinfo (real UTC or timezone.utc)",
    isinstance(real_optouts.UTC, _datetime.tzinfo),
)


section("tools.py recent-hook-errors path has the polyfill in source")
from chameleon_mcp import tools  # noqa: E402

src = inspect.getsource(tools)
t(
    "tools.py contains the UTC polyfill in the recent-hook-errors path",
    "from datetime import UTC as _UTC" in src and "_UTC = _tz.utc" in src,
)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
section("Summary")
print(f"\n  Total: {len(PASS) + len(FAIL)}")
print(f"  Pass: {len(PASS)}")
print(f"  Fail: {len(FAIL)}")
if FAIL:
    print("\n  FAILURES:")
    for name, info in FAIL:
        print(f"    - {name}{(': ' + info) if info else ''}")
    sys.exit(1)
sys.exit(0)
