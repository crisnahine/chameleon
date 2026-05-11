"""Regression tests for BUG-021 and BUG-022:

BUG-021: detect_repo distinguishes profile_corrupted from profile_present
BUG-022: get_pattern_context returns consistent shape on corrupted-profile path

Pre-v0.5.6:
- detect_repo on a repo with a corrupted profile.json still returned
  profile_status='profile_present' and trust_state='untrusted', giving
  the consumer no signal that the profile was unreadable.
- get_pattern_context's corrupted-profile early return used the key
  'name' under archetype where the healthy path uses 'archetype', and
  dropped 'content_signal_match' and the 'idioms' field entirely.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_6_corrupted_profile_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_6_corrupted_data_")
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


from chameleon_mcp.tools import bootstrap_repo, detect_repo, get_pattern_context  # noqa: E402


def _setup(td: str) -> Path:
    root = Path(td)
    (root / "package.json").write_text(
        '{"name":"x","dependencies":{"typescript":"5"}}'
    )
    (root / "tsconfig.json").write_text("{}")
    (root / "a.ts").write_text("export const x = 1;\n")
    bootstrap_repo(str(root))
    return root


def main() -> int:
    print("=== BUG-021/022: corrupted profile detection and response shape ===")

    with tempfile.TemporaryDirectory(prefix="bug021_") as td:
        root = _setup(td)
        # Corrupt profile.json
        (root / ".chameleon" / "profile.json").write_text("{ not json ")

        # BUG-021
        detect_resp = detect_repo(str(root / "a.ts"))
        t(
            "detect_repo reports profile_corrupted",
            detect_resp["data"]["profile_status"] == "profile_corrupted",
            f"got {detect_resp['data']['profile_status']!r}",
        )
        t(
            "trust_state is n/a on corrupted profile",
            detect_resp["data"]["trust_state"] == "n/a",
            f"got {detect_resp['data']['trust_state']!r}",
        )

        # BUG-022
        ctx_resp = get_pattern_context(str(root / "a.ts"))
        data = ctx_resp["data"]
        arch = data.get("archetype", {})
        t(
            "archetype envelope uses 'archetype' key (not 'name')",
            "archetype" in arch and "name" not in arch,
            f"got {list(arch.keys())!r}",
        )
        t(
            "archetype envelope carries content_signal_match",
            "content_signal_match" in arch,
            f"got {list(arch.keys())!r}",
        )
        t(
            "response carries idioms field",
            "idioms" in data,
            f"got {list(data.keys())!r}",
        )
        t(
            "archetype value is null on corrupted profile",
            arch.get("archetype") is None,
            f"got {arch!r}",
        )

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
