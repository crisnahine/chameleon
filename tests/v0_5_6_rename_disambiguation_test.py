"""Regression test for BUG-013: collision disambiguation prefers path segments.

Pre-v0.5.6, when two clusters wanted the same base name, the second one
got a numeric suffix (``react-component-2``, ``service-20``). Users
complained the names didn't help them understand cluster differences.
Now the collision path walks the cluster's path metadata for meaningful
segments and only falls back to a counter when no path tail differs.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_6_rename_disambiguation_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_6_disambig_data_")
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


from chameleon_mcp.bootstrap.naming import (  # noqa: E402
    _disambiguation_suffixes,
    propose_archetype_name,
)


def _mk_cluster(paths_pattern: str, member: str) -> SimpleNamespace:
    """A minimal cluster shape that propose_archetype_name can read."""
    key = SimpleNamespace(
        path_pattern_bucket=paths_pattern,
        default_export_kind=None,
        top_level_node_kinds=(),
        jsx_present=False,
        named_export_count_bucket="0",
        content_signal="none",
        filename_suffix="",
    )
    return SimpleNamespace(
        key=key,
        members=[SimpleNamespace(path=member)],
    )


def main() -> int:
    print("=== BUG-013: collision disambiguation by path segments ===")

    suffixes = _disambiguation_suffixes(_mk_cluster(
        "src/comments/api:ts", "src/features/comments/api/create-comment.ts"
    ))
    t(
        "multiple disambiguator candidates emitted (not just one)",
        len(suffixes) >= 2,
        f"got {suffixes!r}",
    )
    t(
        "candidates include the leaf directory",
        "comments" in suffixes or "features" in suffixes or "api" in suffixes,
        f"got {suffixes!r}",
    )

    # Simulate three react-component clusters in different dirs.
    a = _mk_cluster(
        "src/components/ui/button:tsx", "src/components/ui/button/button.tsx"
    )
    b = _mk_cluster(
        "src/components/ui/icons:tsx", "src/components/ui/icons/cog.tsx"
    )
    c = _mk_cluster(
        "src/components/ui/modal:tsx", "src/components/ui/modal/modal.tsx"
    )

    existing: set[str] = set()
    # Force the base name to collide by claiming "react-component" up front.
    existing.add("react-component")
    # Each cluster's heuristic would normally return None → "cluster-<hash>".
    # We want to verify _disambiguation flow when base collides, so simulate
    # by checking that the produced names carry a path segment, not a digit.
    name_a = propose_archetype_name(a, existing)
    existing.add(name_a)
    name_b = propose_archetype_name(b, existing)
    existing.add(name_b)
    name_c = propose_archetype_name(c, existing)
    existing.add(name_c)

    t(
        "name_a is unique",
        name_a not in {"react-component"},
        f"got {name_a!r}",
    )
    # The point: at least ONE of the produced names should carry a path
    # segment as a disambiguator rather than a numeric suffix. We're not
    # forcing every cluster to collide (depends on heuristic output), but
    # we want to verify the path-segment path is reachable.
    produced = {name_a, name_b, name_c}
    has_path_disambig = any(
        any(seg in n for seg in ("button", "icons", "modal", "ui"))
        for n in produced
    )
    t(
        "at least one produced name carries a path-segment disambiguator",
        has_path_disambig,
        f"got {produced!r}",
    )
    has_pure_counter = any(
        n.rsplit("-", 1)[-1].isdigit() and n.rsplit("-", 1)[-1] != ""
        and not any(seg in n for seg in ("button", "icons", "modal", "ui"))
        for n in produced
    )
    t(
        "no pure ``<base>-<digit>`` name when path segments are available",
        not has_pure_counter,
        f"got {produced!r}",
    )

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
