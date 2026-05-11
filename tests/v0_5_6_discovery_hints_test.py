"""Regression test for BUG-001: ad-hoc monorepo discovery hints.

Pre-v0.5.6, bootstrap_repo on bulletproof-react (root pkg.json has no
TS deps and no workspaces field, but apps/*/package.json carries TS
deps) returned failed_unsupported_language with no guidance. The user
had to know to bootstrap each app individually.

Now: the failure envelope carries a discovery_hints array listing
each app/* or packages/* child that has its own package.json or
Gemfile.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_6_discovery_hints_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_6_hints_data_")
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
    print("=== BUG-001: ad-hoc monorepo discovery_hints ===")

    # Scenario: an ad-hoc monorepo where each app is RUBY (not TS). The
    # TS workspace detector won't find anything (Ruby workspaces are not
    # what that path looks for). We expect failed_unsupported_language at
    # the root + discovery_hints listing each Ruby sub-app.
    with tempfile.TemporaryDirectory(prefix="bug001_") as td:
        root = Path(td)
        # Root has nothing — no package.json, no Gemfile, no tsconfig.
        (root / "README.md").write_text("# monorepo")
        for app in ("api", "worker"):
            d = root / "apps" / app
            d.mkdir(parents=True)
            (d / "Gemfile").write_text('source "https://rubygems.org"\n')
            (d / "main.rb").write_text(f"# {app}\nputs 1\n")
        # Also a Ruby package
        rb = root / "packages" / "shared"
        rb.mkdir(parents=True)
        (rb / "Gemfile").write_text('source "https://rubygems.org"\n')
        (rb / "lib.rb").write_text("module Shared\nend\n")

        resp = bootstrap_repo(str(root))
        data = resp["data"]
        t(
            "root bootstrap returns failed_unsupported_language",
            data.get("status") == "failed_unsupported_language",
            f"got {data.get('status')!r}",
        )
        hints = data.get("discovery_hints") or []
        t(
            "discovery_hints lists apps/api",
            any(h.get("subdir") == "apps/api" for h in hints),
            f"got {hints!r}",
        )
        t(
            "discovery_hints lists apps/worker",
            any(h.get("subdir") == "apps/worker" for h in hints),
            f"got {hints!r}",
        )
        t(
            "discovery_hints lists packages/shared",
            any(h.get("subdir") == "packages/shared" for h in hints),
            f"got {hints!r}",
        )
        ruby_hint = next(
            (h for h in hints if h.get("subdir") == "packages/shared"),
            None,
        )
        t(
            "Ruby hint marked language=ruby",
            ruby_hint and ruby_hint.get("language") == "ruby",
            f"got {ruby_hint!r}",
        )

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
