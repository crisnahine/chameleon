"""Regression test for BUG-019: sidecar bootstrap inherits parent signals.

Pre-v0.5.6, language_hint.note said:
    "Run bootstrap_repo(<rails-repo>/app/javascript) for the JS half"
but that call returned status='failed_unsupported_language' because the
sidecar dir had no own package.json / tsconfig. The recommendation was
a trap.

Now: when bootstrap_repo is invoked on a sidecar (no own package.json /
tsconfig but a parent up to 4 levels above carries one), the bootstrap
inherits the parent's tool configs and the parent's extractor decision.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_6_sidecar_bootstrap_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_6_sidecar_data_")
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


from chameleon_mcp.tools import bootstrap_repo  # noqa: E402


def main() -> int:
    print("=== BUG-019: sidecar bootstrap inherits parent signals ===")

    with tempfile.TemporaryDirectory(prefix="bug019_") as td:
        root = Path(td)
        # Rails-with-frontend layout: root has Gemfile + package.json with TS deps
        (root / "Gemfile").write_text('source "https://rubygems.org"\ngem "rails"\n')
        (root / "package.json").write_text(
            '{"name":"x","dependencies":{"typescript":"5"}}'
        )
        (root / "tsconfig.json").write_text("{}")
        (root / ".prettierrc").write_text('{"singleQuote":true,"semi":false}')
        # JS sidecar under app/javascript with no own package.json
        sidecar = root / "app" / "javascript"
        sidecar.mkdir(parents=True)
        for i in range(6):
            (sidecar / f"f{i}.ts").write_text(
                "import { foo } from './bar';\nexport const v"
                + str(i)
                + " = "
                + str(i)
                + ";\n"
            )

        resp = bootstrap_repo(str(sidecar))
        data = resp["data"]
        t(
            "sidecar bootstrap succeeds (not failed_unsupported_language)",
            data.get("status") == "success",
            f"got status={data.get('status')!r} error={data.get('error')!r}",
        )

        # rules.json should now carry the inherited prettier config
        rules_path = sidecar / ".chameleon" / "rules.json"
        t(
            "sidecar profile written",
            rules_path.is_file(),
            f"rules.json missing at {rules_path}",
        )
        if rules_path.is_file():
            import json
            rules = json.loads(rules_path.read_text())["rules"]
            t(
                "inherited prettier rules carry through",
                "formatting" in rules and rules["formatting"]["rules"].get("singleQuote") is True,
                f"got rules={rules!r}",
            )

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
