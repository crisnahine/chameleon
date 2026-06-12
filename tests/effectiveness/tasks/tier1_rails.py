"""Tier-1 (ci) Rails tasks against the committed eff_rails fixture."""

from __future__ import annotations

import re
from pathlib import Path

from tests.effectiveness.tasks import EffTask

TASKS = [
    EffTask(
        task_id="t1-rails-convention-service",
        tier="ci",
        fixture="rails",
        prompt=(
            "Add the ability to cancel an order: cancelling sets the order's "
            "cancelled_at timestamp and refuses to cancel an already-cancelled "
            "order. Implement it the way this codebase implements business "
            "actions, then make sure the project's checks still pass."
        ),
        category="convention",
        scorers=("convention", "verification", "cost"),
    ),
    EffTask(
        task_id="t1-rails-crossfile-rename",
        tier="ci",
        fixture="rails",
        prompt=(
            "Rename MoneyFormatter.format to MoneyFormatter.display everywhere "
            "in this repo (definition and every call site), keeping behavior "
            "identical. Run the tests when you are done."
        ),
        category="crossfile",
        scorers=("crossfile", "convention", "verification", "cost"),
    ),
    EffTask(
        task_id="t1-rails-duplication-email",
        tier="ci",
        fixture="rails",
        prompt=(
            "User registration currently stores emails exactly as submitted, "
            "so ' Bob@Example.COM ' and 'bob@example.com' create duplicate "
            "accounts. Make RegisterUser normalize emails before creating the "
            "user. Keep the diff small and run the tests."
        ),
        category="duplication",
        scorers=("duplication", "convention", "verification", "cost"),
    ),
    EffTask(
        task_id="t1-rails-verification-refund",
        tier="ci",
        fixture="rails",
        prompt=(
            "Customer support reports refunds coming out wrong for orders "
            "older than the full-refund window. Find the bug, fix it, and "
            "prove the fix by running the project's tests."
        ),
        category="verification",
        scorers=("verification", "convention", "cost"),
        setup="plant_refund_bug",
    ),
]

CROSSFILE_TARGETS = {
    "t1-rails-crossfile-rename": {
        "module": "app/lib/money_formatter.rb",
        "function": "format",
        "new_name": "display",
        # Qualified call form for the staleness grep: the fixture's test
        # labels contain the bare word "format", which must never read as
        # a stale caller.
        "old_needle": "MoneyFormatter.format",
    },
}

DUPLICATION_TARGETS = {
    "t1-rails-duplication-email": {
        "existing_name": "normalize",
        "existing_file": "app/lib/email_normalizer.rb",
        "needle": "EmailNormalizer",
    },
}


def _rubric_cancel_order(worktree: Path) -> dict:
    """Service-object conventions: placement, module wrap, #call, Result."""
    svc = worktree / "app" / "services" / "orders" / "cancel_order.rb"
    out = {
        "service_file_created": svc.is_file(),
        "module_wrapped": False,
        "has_call_method": False,
        "returns_result": False,
    }
    if not svc.is_file():
        return out
    text = svc.read_text(encoding="utf-8", errors="replace")
    out["module_wrapped"] = re.search(r"^module Orders", text, re.MULTILINE) is not None
    out["has_call_method"] = re.search(r"^\s*def call\b", text, re.MULTILINE) is not None
    out["returns_result"] = "Result.success" in text and "Result.failure" in text
    return out


RUBRICS = {
    "t1-rails-convention-service": _rubric_cancel_order,
}


def _plant_refund_bug(worktree: Path) -> None:
    """Partial refunds pay the FULL amount: drop the rate multiplication."""
    p = worktree / "app" / "lib" / "refund_calculator.rb"
    text = p.read_text(encoding="utf-8")
    broken = text.replace(
        "(total_cents * PARTIAL_REFUND_RATE).floor",
        "total_cents",
    )
    if broken == text:
        raise RuntimeError("refund_calculator.rb did not match the expected pristine body")
    p.write_text(broken, encoding="utf-8")


SETUPS = {
    "plant_refund_bug": _plant_refund_bug,
}
