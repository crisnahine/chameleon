"""Auto-discover SCENARIOS from sibling family modules.

Naming convention:
  <family>.py            -> discovered automatically (public scenario family)
  _init_placeholder.py   -> included explicitly as the framework smoke stub
  _<anything-else>.py    -> skipped (private helpers, __pycache__, etc.)
"""
from __future__ import annotations

import importlib
import pkgutil

from tests.dogfood.scenario import Scenario

# Placeholder modules that use a leading _ but should still be discovered.
# Add names here only for stub files that are waiting to be replaced by a
# real family module.
_PLACEHOLDER_MODULES: set[str] = set()


def all_scenarios() -> list[Scenario]:
    out: list[Scenario] = []
    for mod_info in pkgutil.iter_modules(__path__):
        name = mod_info.name
        if name.startswith("_") and name not in _PLACEHOLDER_MODULES:
            continue
        mod = importlib.import_module(f"{__name__}.{name}")
        out.extend(getattr(mod, "SCENARIOS", []))
    return out
