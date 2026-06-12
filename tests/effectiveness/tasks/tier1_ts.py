"""Tier-1 (ci) TypeScript tasks against the committed eff_ts fixture.

Prompts sit where conventions are non-obvious or cross-file (pilot lesson:
clean-repo style tasks measure nothing). Crossfile targets were picked FROM
the fixture's bootstrapped calls_index (formatMoney: 4 import-grade callers);
test_fixture_validation.py re-derives and re-checks that on every run.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.effectiveness.tasks import EffTask

TASKS = [
    EffTask(
        task_id="t1-ts-convention-component",
        tier="ci",
        fixture="ts",
        prompt=(
            "Add a StockBadge component that shows a product's stock level. "
            "It takes the number of units in stock and renders 'Out of stock' "
            "when zero, 'Low stock' under 5 units, and 'In stock' otherwise. "
            "Make it fit this codebase's existing component conventions, then "
            "make sure the project's checks still pass."
        ),
        category="convention",
        scorers=("convention", "verification", "cost"),
    ),
    EffTask(
        task_id="t1-ts-crossfile-rename",
        tier="ci",
        fixture="ts",
        prompt=(
            "Rename the formatMoney function to formatCurrency everywhere in "
            "this repo (definition and every usage), keeping behavior "
            "identical. Run the tests when you are done."
        ),
        category="crossfile",
        scorers=("crossfile", "convention", "verification", "cost"),
    ),
    EffTask(
        task_id="t1-ts-duplication-slug",
        tier="ci",
        fixture="ts",
        prompt=(
            "Product pages need URL-safe slugs for product names (for links "
            "like /products/<slug>). Add that capability to ProductCard: the "
            "card's heading should link to /products/<slug-of-name>. Keep the "
            "diff small and run the tests."
        ),
        category="duplication",
        scorers=("duplication", "convention", "verification", "cost"),
    ),
    EffTask(
        task_id="t1-ts-verification-clamp",
        tier="ci",
        fixture="ts",
        prompt=(
            "Quantity clamping is broken: fetchQuote sometimes requests "
            "quantities outside the 1-99 range. Find the bug, fix it, and "
            "prove the fix by running the project's tests."
        ),
        category="verification",
        scorers=("verification", "convention", "cost"),
        setup="plant_clamp_bug",
    ),
]

CROSSFILE_TARGETS = {
    "t1-ts-crossfile-rename": {
        "module": "src/utils/format_money.ts",
        "function": "formatMoney",
        "new_name": "formatCurrency",
        # Qualified call form for the staleness grep: the bare name also
        # appears in prose (test labels), which must never read as stale.
        "old_needle": "formatMoney(",
    },
}

DUPLICATION_TARGETS = {
    "t1-ts-duplication-slug": {
        "existing_name": "slugify",
        "existing_file": "src/utils/slugify.ts",
        "needle": "slugify",
    },
}


def _rubric_stock_badge(worktree: Path) -> dict:
    """Placement dir, naming pattern, props-type + named-export conventions."""
    comp_dir = worktree / "src" / "components"
    candidates = [
        p
        for p in (comp_dir.glob("*.tsx") if comp_dir.is_dir() else [])
        if "stock" in p.name.lower()
    ]
    out = {
        "placed_in_components": bool(candidates),
        "pascal_case_filename": False,
        "has_props_type": False,
        "named_export_no_default": False,
    }
    if not candidates:
        return out
    f = candidates[0]
    out["pascal_case_filename"] = re.fullmatch(r"[A-Z][A-Za-z0-9]*\.tsx", f.name) is not None
    text = f.read_text(encoding="utf-8", errors="replace")
    out["has_props_type"] = re.search(r"(type|interface)\s+\w*Props", text) is not None
    out["named_export_no_default"] = "export function" in text and "export default" not in text
    return out


RUBRICS = {
    "t1-ts-convention-component": _rubric_stock_badge,
}


def _plant_clamp_bug(worktree: Path) -> None:
    """Swap min/max so clamp returns out-of-range values; tests then fail."""
    p = worktree / "src" / "utils" / "clamp.ts"
    text = p.read_text(encoding="utf-8")
    broken = text.replace(
        "return Math.min(Math.max(value, min), max);",
        "return Math.max(Math.min(value, min), max);",
    )
    if broken == text:
        raise RuntimeError("clamp.ts did not match the expected pristine body")
    p.write_text(broken, encoding="utf-8")


SETUPS = {
    "plant_clamp_bug": _plant_clamp_bug,
}
