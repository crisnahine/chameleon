"""Verification of BUG-014 (continued): rubocop extracts for pure-Ruby repos.

Pre-v0.5.7 the BUG-019 sidecar walk-up logic fired on any repo without
package.json/tsconfig.json — including pure-Ruby repos that DO have a
Gemfile in themselves. The walk-up then grabbed an unrelated ancestor
(e.g. /Users/<name>) and ran read_tool_configs there, discarding the
actual .rubocop.yml at the Ruby repo root.

Fix: gate the walk-up on "no own JS signals AND no own Ruby signals".
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PASS, FAIL = [], []


def t(name, condition, info=""):
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' — ' + info) if info else ''}")


def section(title):
    print(f"\n=== {title} ===")


from chameleon_mcp.tools import bootstrap_repo

# ---------------------------------------------------------------------------
# Case 1: pure-Ruby repo with .rubocop.yml — should extract
# ---------------------------------------------------------------------------
section("Pure-Ruby repo with .rubocop.yml")

with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp) / "rails-app"
    repo.mkdir()
    (repo / "Gemfile").write_text("source 'https://rubygems.org'\ngem 'rails'\n")
    (repo / ".rubocop.yml").write_text("""
require:
  - rubocop-rails

AllCops:
  TargetRubyVersion: 3.2
  NewCops: enable

Style/Documentation:
  Enabled: false
""")
    appdir = repo / "app" / "controllers"
    appdir.mkdir(parents=True)
    for i in range(4):
        (appdir / f"thing{i}_controller.rb").write_text(f"# frozen_string_literal: true\nclass Thing{i}Controller < ApplicationController\n  def index; end\nend\n")

    resp = bootstrap_repo(str(repo))
    status = resp["data"]["status"]
    t("bootstrap status success", status == "success", f"got {status}")

    rules_path = repo / ".chameleon" / "rules.json"
    rj = json.loads(rules_path.read_text())
    keys = list(rj.get("rules", {}).keys())
    t("rules.json contains 'rubocop' key", "rubocop" in keys, f"keys={keys}")
    if "rubocop" in keys:
        rubocop_rule = rj["rules"]["rubocop"]
        t("rubocop rule has source",
          "source" in rubocop_rule,
          f"source={rubocop_rule.get('source')}")
        t("rubocop rules captured",
          isinstance(rubocop_rule.get("rules"), dict) and "AllCops" in rubocop_rule["rules"],
          f"rules keys={list((rubocop_rule.get('rules') or {}).keys())[:5]}")

# ---------------------------------------------------------------------------
# Case 2: Real ef-api fixture (uses inherit_from in its .rubocop.yml)
# ---------------------------------------------------------------------------
section("ef-api real fixture")
# Set CHAMELEON_TEST_APPS_DIR to a directory containing an `ef-api`
# checkout to exercise this; skips gracefully when unset or absent.
_apps_dir = os.environ.get("CHAMELEON_TEST_APPS_DIR")
ef = Path(_apps_dir) / "ef-api" if _apps_dir else None
if ef and ef.is_dir() and (ef / ".rubocop.yml").is_file():
    shutil.rmtree(ef / ".chameleon", ignore_errors=True)
    resp = bootstrap_repo(str(ef))
    t("ef-api bootstrap success", resp["data"]["status"] == "success",
      f"got {resp['data']['status']}: {resp['data'].get('error')}")
    rj = json.loads((ef / ".chameleon" / "rules.json").read_text())
    t("ef-api rules.json has rubocop", "rubocop" in rj.get("rules", {}),
      f"keys={list(rj.get('rules', {}).keys())}")
else:
    print("  (skip — ef-api not present)")

print(f"\n=== Summary: {len(PASS)} pass, {len(FAIL)} fail ===")
if FAIL:
    for name, info in FAIL:
        print(f"  FAIL: {name} — {info}")
    sys.exit(1)
