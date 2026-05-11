"""Regression test for BUG-024: preflight hook gates injection on trust state.

Pre-v0.5.6 the preflight-and-advise hook injected the full canonical
witness even when trust_state was 'untrusted', contradicting the
using-chameleon skill rule. Now:
  - First call in a session: emit a one-time trust prompt.
  - Subsequent calls in same session: stay silent.
  - Once trust is granted: regular canonical injection resumes.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_6_preflight_trust_gate_test.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parent.parent
HOOKS = REPO_ROOT / "hooks"
sys.path.insert(0, str(REPO_ROOT / "mcp"))

TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_6_trust_gate_data_")
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


def _call_hook(file_path: Path, session_id: str) -> dict:
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT)
    payload = json.dumps({
        "tool_name": "Edit",
        "tool_input": {"file_path": str(file_path)},
        "session_id": session_id,
    })
    proc = subprocess.run(
        [str(HOOKS / "preflight-and-advise")],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def main() -> int:
    print("=== BUG-024: preflight hook gates injection on untrusted profile ===")
    from chameleon_mcp.tools import bootstrap_repo

    with tempfile.TemporaryDirectory(prefix="bug024_") as td:
        root = Path(td)
        (root / "package.json").write_text(
            '{"name":"x","dependencies":{"typescript":"5"}}'
        )
        (root / "tsconfig.json").write_text("{}")
        # Several similar files so clustering picks up something
        for i in range(6):
            (root / f"f{i}.ts").write_text(
                "import { foo } from './bar';\nexport const v"
                + str(i)
                + " = "
                + str(i)
                + ";\n"
            )
        boot = bootstrap_repo(str(root))
        t(
            "bootstrap success",
            boot["data"].get("status") == "success",
            f"got {boot['data']!r}",
        )

        target = root / "f0.ts"

        # First call in session: should surface the trust prompt
        first = _call_hook(target, session_id="sess-A")
        first_ctx = (first.get("hookSpecificOutput") or {}).get("additionalContext", "")
        t(
            "first untrusted call surfaces trust prompt",
            "untrusted" in first_ctx and "/chameleon-trust" in first_ctx,
            f"got first_ctx={first_ctx[:200]!r}",
        )
        t(
            "first untrusted call does NOT inject canonical witness",
            "Canonical witness" not in first_ctx,
            f"got first_ctx={first_ctx[:200]!r}",
        )

        # Second call in SAME session: should stay silent
        second = _call_hook(target, session_id="sess-A")
        second_ctx = (second.get("hookSpecificOutput") or {}).get("additionalContext", "")
        t(
            "second untrusted call in same session is silent",
            not second_ctx,
            f"got second_ctx={second_ctx[:200]!r}",
        )

        # Different session: should prompt again
        third = _call_hook(target, session_id="sess-B")
        third_ctx = (third.get("hookSpecificOutput") or {}).get("additionalContext", "")
        t(
            "different session sees the trust prompt",
            "untrusted" in third_ctx and "/chameleon-trust" in third_ctx,
            f"got third_ctx={third_ctx[:200]!r}",
        )

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
