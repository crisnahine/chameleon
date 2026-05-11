"""Regression test for BUG-005: trust_state should be 'n/a' when no profile.

Pre-v0.5.6, detect_repo on a repo without a profile returned
trust_state='untrusted'. The schema docstring says n/a is the right value
when there is no profile. Untrusted implied a profile existed that the
user hadn't trusted, which was misleading.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_6_trust_state_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_6_trust_state_data_")
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


from chameleon_mcp.tools import detect_repo  # noqa: E402


def main() -> int:
    print("=== BUG-005: trust_state is 'n/a' when no profile ===")
    with tempfile.TemporaryDirectory(prefix="bug005_") as td:
        root = Path(td)
        (root / "package.json").write_text(
            '{"name":"x","dependencies":{"typescript":"5"}}'
        )
        (root / "tsconfig.json").write_text("{}")
        sample = root / "a.ts"
        sample.write_text("export const x = 1;\n")

        resp = detect_repo(str(sample))
        data = resp["data"]
        t(
            "profile_status is no_profile",
            data["profile_status"] == "no_profile",
            f"got {data['profile_status']!r}",
        )
        t(
            "trust_state is n/a (not 'untrusted') when no profile",
            data["trust_state"] == "n/a",
            f"got {data['trust_state']!r}",
        )

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
