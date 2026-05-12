"""Verification of BUG-019 (continued): sidecar bootstrap picks correct extractor.

Pre-v0.5.7 the sidecar code-path inherited the parent's extractor, so a
JS sidecar (e.g. forem/app/javascript) inside a Rails-primary repo got
RubyExtractor → glob "**/*.rb" → 0 files matched → failed_unsupported_language.

Fix: count source files inside the sidecar and pick the dominant extractor.
"""

import json
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
# Case 1: JS sidecar of Rails-with-frontend repo (forem layout)
# ---------------------------------------------------------------------------
section("JS sidecar of Rails-with-frontend repo")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "rails-app"
    root.mkdir()
    (root / "Gemfile").write_text("source 'https://rubygems.org'\ngem 'rails'\n")
    (root / "config").mkdir()
    (root / "config" / "application.rb").write_text("module App; class Application < Rails::Application; end; end\n")
    (root / "package.json").write_text('{"name":"x","dependencies":{}}')

    js = root / "app" / "javascript"
    js.mkdir(parents=True)
    for i in range(5):
        (js / f"feature{i}.jsx").write_text(f"export const Foo{i} = () => null;")

    resp = bootstrap_repo(str(js))
    t("sidecar bootstrap succeeds", resp["data"]["status"] == "success",
      f"got status={resp['data']['status']} error={resp['data'].get('error')}")
    t("sidecar processed > 0 files",
      resp["data"].get("files_processed", 0) > 0,
      f"files_processed={resp['data'].get('files_processed')}")

# ---------------------------------------------------------------------------
# Case 2: Ruby sidecar of TS repo (rare but possible)
# ---------------------------------------------------------------------------
section("Ruby sidecar inside a TS repo")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "ts-app"
    root.mkdir()
    (root / "package.json").write_text('{"name":"x","dependencies":{"typescript":"5"}}')
    (root / "tsconfig.json").write_text("{}")
    (root / "src").mkdir()
    (root / "src" / "main.ts").write_text("export const x = 1;")

    rb = root / "scripts"
    rb.mkdir()
    for i in range(5):
        (rb / f"task{i}.rb").write_text(f"# frozen_string_literal: true\nputs '{i}'")

    resp = bootstrap_repo(str(rb))
    t("ruby sidecar bootstrap succeeds", resp["data"]["status"] == "success",
      f"got status={resp['data']['status']} error={resp['data'].get('error')}")

# ---------------------------------------------------------------------------
# Case 3: Real forem fixture
# ---------------------------------------------------------------------------
section("Real forem fixture")
forem_js = Path("/Users/crisn/Documents/Projects/Testing Apps/forem/app/javascript")
if forem_js.is_dir():
    shutil.rmtree(forem_js / ".chameleon", ignore_errors=True)
    resp = bootstrap_repo(str(forem_js))
    t("forem/app/javascript bootstrap succeeds", resp["data"]["status"] == "success",
      f"got status={resp['data']['status']} error={resp['data'].get('error')}")
    t("forem/app/javascript processed source files",
      resp["data"].get("files_processed", 0) >= 100,
      f"files_processed={resp['data'].get('files_processed')}")
else:
    print("  (skip — forem not present)")

print(f"\n=== Summary: {len(PASS)} pass, {len(FAIL)} fail ===")
if FAIL:
    for name, info in FAIL:
        print(f"  FAIL: {name} — {info}")
    sys.exit(1)
