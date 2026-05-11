"""Regression test for BUG-015: archetype prefix-overlap fallback.

A file at app/controllers/application_controller.rb (the Rails
ApplicationController, the most generally-useful file to know "what
do controllers look like here") returned archetype: null pre-v0.5.6
because the ``controller`` archetype's paths_pattern was
``app/controllers/v1`` and neither exact-bucket nor substring matched.

Now we fall back to longest-shared-directory-prefix with low
confidence so the model gets at least some archetype guidance.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_6_archetype_prefix_fallback_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_6_prefix_data_")
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


from chameleon_mcp.tools import _prefix_overlap_fallback  # noqa: E402


def main() -> int:
    print("=== BUG-015: archetype prefix-overlap fallback ===")

    archetypes = {
        "controller": {
            "paths_pattern": "app/controllers/v1",
            "cluster_size": 50,
        },
        "service": {
            "paths_pattern": "app/services",
            "cluster_size": 30,
        },
        "model": {
            "paths_pattern": "app/models",
            "cluster_size": 100,
        },
    }

    primary, alts = _prefix_overlap_fallback(
        "app/controllers/application_controller.rb", archetypes
    )
    t(
        "application_controller falls back to controller archetype",
        primary == "controller",
        f"got {primary!r} (alts={alts!r})",
    )

    primary2, _ = _prefix_overlap_fallback(
        "app/services/notifications/send_email.rb", archetypes
    )
    t(
        "deeper service file falls back to service archetype",
        primary2 == "service",
        f"got {primary2!r}",
    )

    primary3, _ = _prefix_overlap_fallback(
        "lib/something/unrelated.rb", archetypes
    )
    t(
        "no shared prefix → returns None",
        primary3 is None,
        f"got {primary3!r}",
    )

    # Extension mismatch: file is .rb but only TS archetypes exist
    ts_archetypes = {
        "react-component": {
            "paths_pattern": "src/components:tsx",
            "cluster_size": 10,
        },
    }
    primary4, _ = _prefix_overlap_fallback(
        "src/components/foo.rb", ts_archetypes
    )
    t(
        "extension mismatch (rb vs tsx) excluded from fallback",
        primary4 is None,
        f"got {primary4!r}",
    )

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
