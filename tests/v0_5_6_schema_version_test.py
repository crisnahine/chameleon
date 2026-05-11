"""Regression test for BUG-023: refuse profiles with newer schema_version.

Pre-v0.5.6, the loader checked engine_min_version but not schema_version,
so a profile marked schema_version=99 was silently accepted. If a future
chameleon ships schema v8 with new fields, a v0.5.x client would silently
read it and may emit wrong data because it doesn't know the new fields.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_6_schema_version_test.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_6_schema_data_")
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


from chameleon_mcp.profile.loader import ProfileLoadError, load_profile_dir  # noqa: E402
from chameleon_mcp.tools import bootstrap_repo  # noqa: E402


def main() -> int:
    print("=== BUG-023: schema_version too-new refusal ===")

    with tempfile.TemporaryDirectory(prefix="bug023_") as td:
        root = Path(td)
        (root / "package.json").write_text(
            '{"name":"x","dependencies":{"typescript":"5"}}'
        )
        (root / "tsconfig.json").write_text("{}")
        (root / "a.ts").write_text("export const x = 1;\n")
        bootstrap_repo(str(root))

        # Sanity: profile loads at the current schema
        loaded = load_profile_dir(root / ".chameleon")
        t("baseline load succeeds", loaded.profile.get("schema_version") is not None)

        # Bump schema_version to 99 in profile.json
        profile_path = root / ".chameleon" / "profile.json"
        data = json.loads(profile_path.read_text())
        data["schema_version"] = 99
        profile_path.write_text(json.dumps(data))

        try:
            load_profile_dir(root / ".chameleon")
            t("loader refused too-new schema_version", False, "no error raised")
        except ProfileLoadError as e:
            msg = str(e).lower()
            t(
                "loader refused too-new schema_version",
                "schema" in msg and "99" in msg,
                f"got {e!s}",
            )

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
