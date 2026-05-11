"""Regression test for BUG-014: rubocop extractor reads .rubocop.yml.

Pre-v0.5.6 all Ruby test repos returned rules: {} because chameleon had
no Ruby-tool extractor. Editing a .rb file got zero linting guidance
from the team's actual style.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_6_rubocop_extractor_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_6_rubocop_data_")
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


from chameleon_mcp.bootstrap.tool_config import read_tool_configs  # noqa: E402
from chameleon_mcp.tools import bootstrap_repo  # noqa: E402


def main() -> int:
    print("=== BUG-014: rubocop extractor reads .rubocop.yml ===")

    fixture_root = HERE.parent / "fixtures" / "rubocop_yml"
    cfg = read_tool_configs(fixture_root)
    t(
        "read_tool_configs.rubocop populated",
        isinstance(cfg.rubocop, dict),
        f"got {cfg.rubocop!r}",
    )
    if cfg.rubocop:
        t(
            "captures plugins list",
            cfg.rubocop.get("plugins") == ["rubocop-rspec", "rubocop-rails"],
            f"got {cfg.rubocop.get('plugins')!r}",
        )
        t(
            "captures AllCops mapping",
            isinstance(cfg.rubocop.get("AllCops"), dict)
            and cfg.rubocop["AllCops"].get("DisabledByDefault") is False,
            f"got {cfg.rubocop.get('AllCops')!r}",
        )
        t(
            "captures individual cops",
            cfg.rubocop.get("Style/StringLiterals", {}).get("EnforcedStyle")
            == "single_quotes",
            f"got {cfg.rubocop.get('Style/StringLiterals')!r}",
        )

    # Bootstrap a Ruby repo that includes .rubocop.yml; rules.json should
    # carry the rubocop block.
    with tempfile.TemporaryDirectory(prefix="bug014_") as td:
        root = Path(td)
        (root / "Gemfile").write_text('source "https://rubygems.org"\ngem "rails"\n')
        shutil.copy(fixture_root / ".rubocop.yml", root / ".rubocop.yml")
        # Need at least one Ruby file or bootstrap fails
        (root / "app").mkdir()
        for i in range(4):
            (root / "app" / f"foo{i}.rb").write_text(
                f"# frozen_string_literal: true\nclass Foo{i}\n  def bar\n    {i}\n  end\nend\n"
            )
        resp = bootstrap_repo(str(root))
        data = resp["data"]
        t(
            "bootstrap success",
            data.get("status") == "success",
            f"got status={data.get('status')} error={data.get('error')}",
        )

        rules_path = root / ".chameleon" / "rules.json"
        t("rules.json written", rules_path.is_file())
        if rules_path.is_file():
            rules = json.loads(rules_path.read_text())["rules"]
            t(
                "rules.json carries a rubocop block",
                "rubocop" in rules,
                f"got rules keys={list(rules.keys())!r}",
            )

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
