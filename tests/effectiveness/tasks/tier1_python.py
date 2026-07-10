"""Tier-1 (ci) Python tasks against the committed eff_py fixture.

Prompts sit where conventions are non-obvious or cross-file (pilot lesson:
clean-repo style tasks measure nothing). Crossfile targets were picked FROM
the fixture's bootstrapped calls_index (format_money: 4 import-grade caller
files); test_fixture_validation.py re-derives and re-checks that on every run.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.effectiveness.tasks import EffTask

TASKS = [
    EffTask(
        task_id="t1-py-convention-service",
        tier="ci",
        fixture="py",
        prompt=(
            "Add a wishlist capability: a user can add a product to their "
            "wishlist and list the products in it. Implement the domain "
            "logic the way this codebase implements it and expose it over "
            "HTTP the way existing features are exposed, then make sure the "
            "project's checks still pass."
        ),
        category="convention",
        scorers=("convention", "verification", "cost"),
    ),
    EffTask(
        task_id="t1-py-crossfile-rename",
        tier="ci",
        fixture="py",
        prompt=(
            "Rename the format_money function to format_currency everywhere "
            "in this repo (definition and every usage), keeping behavior "
            "identical. Run the tests when you are done."
        ),
        category="crossfile",
        scorers=("crossfile", "convention", "verification", "cost"),
    ),
    EffTask(
        task_id="t1-py-duplication-helper",
        tier="ci",
        fixture="py",
        prompt=(
            "Product pages need URL-safe slugs for product names (for links "
            "like /products/<slug>). Add that capability to the products "
            "API: each product payload it returns should include a slug of "
            "the product name. Keep the diff small and run the tests."
        ),
        category="duplication",
        scorers=("duplication", "convention", "verification", "cost"),
    ),
    EffTask(
        task_id="t1-py-verification-clamp",
        tier="ci",
        fixture="py",
        prompt=(
            "Order quantities are broken: place_order records a quantity of "
            "99 no matter what the customer asked for. Find the bug, fix it, "
            "and prove the fix by running the project's tests."
        ),
        category="verification",
        scorers=("verification", "convention", "cost"),
        setup="plant_py_clamp_bug",
    ),
]

CROSSFILE_TARGETS = {
    "t1-py-crossfile-rename": {
        "module": "app/utils/money.py",
        "function": "format_money",
        "new_name": "format_currency",
        # Qualified call form for the staleness grep: the bare name also
        # appears in import lines and prose, which must never read as stale.
        "old_needle": "format_money(",
    },
}

DUPLICATION_TARGETS = {
    "t1-py-duplication-helper": {
        "existing_name": "slugify",
        "existing_file": "app/utils/slugs.py",
        "needle": "slugify",
    },
}


def _rubric_wishlist_service(worktree: Path) -> dict:
    """Placement dir, naming pattern, service-class + provider conventions."""
    svc_dir = worktree / "app" / "services"
    candidates = [
        p
        for p in (svc_dir.glob("*.py") if svc_dir.is_dir() else [])
        if "wishlist" in p.name.lower()
    ]
    out = {
        "placed_in_services": bool(candidates),
        "snake_case_service_filename": False,
        "service_class_defined": False,
        "provider_function_defined": False,
    }
    if not candidates:
        return out
    f = candidates[0]
    out["snake_case_service_filename"] = (
        re.fullmatch(r"[a-z][a-z0-9_]*_service\.py", f.name) is not None
    )
    text = f.read_text(encoding="utf-8", errors="replace")
    out["service_class_defined"] = re.search(r"^class \w*Service\b", text, re.MULTILINE) is not None
    out["provider_function_defined"] = (
        re.search(r"^def get_\w*service\(", text, re.MULTILINE) is not None
    )
    return out


RUBRICS = {
    "t1-py-convention-service": _rubric_wishlist_service,
}


def _plant_py_clamp_bug(worktree: Path) -> None:
    """Swap min/max so clamp always returns the high bound; tests then fail."""
    p = worktree / "app" / "utils" / "clamp.py"
    text = p.read_text(encoding="utf-8")
    broken = text.replace(
        "return min(max(value, low), high)",
        "return max(min(value, low), high)",
    )
    if broken == text:
        raise RuntimeError("clamp.py did not match the expected pristine body")
    p.write_text(broken, encoding="utf-8")


SETUPS = {
    "plant_py_clamp_bug": _plant_py_clamp_bug,
}
