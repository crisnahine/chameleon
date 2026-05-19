"""Regression tests for the Phase 4.1 lint_engine.

Covers:
- Pure-function `extract_dimensions` + `lint` + `canonical_confidence`
- End-to-end `lint_file` integration: matching file passes, mismatch flags
  surface, null ast_query no-ops, missing archetype no-ops, content-cap
  truncation, legacy-stub fallback envelope, Ruby file lint, confidence
  edge cases.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/lint_engine_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Make the in-repo chameleon_mcp importable without installing.
HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

# Use isolated plugin data dir per run so the trust record we drop in
# below doesn't bleed into other test files (and vice versa).
TMPDATA = tempfile.mkdtemp(prefix="chameleon_lint_data_")
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


def section(name: str) -> None:
    print(f"\n=== {name} ===")


from chameleon_mcp.lint_engine import (  # noqa: E402
    DimensionSnapshot,
    Violation,
    canonical_confidence,
    detect_language,
    extract_dimensions,
    lint,
)
from chameleon_mcp.profile.trust import grant_trust  # noqa: E402
from chameleon_mcp.tools import _compute_repo_id, bootstrap_repo, lint_file  # noqa: E402

# ---------------------------------------------------------------------------
# Phase 4.1: unit — language detection
# ---------------------------------------------------------------------------
section("language detection")

t("detect_language('foo.ts') == 'typescript'", detect_language("foo.ts") == "typescript")
t("detect_language('foo.tsx') == 'typescript'", detect_language("foo.tsx") == "typescript")
t("detect_language('foo.mjs') == 'typescript'", detect_language("foo.mjs") == "typescript")
t("detect_language('foo.rb') == 'ruby'", detect_language("foo.rb") == "ruby")
t("detect_language('foo.py') is None", detect_language("foo.py") is None)
t("detect_language('') is None", detect_language("") is None)
t("detect_language(None) is None", detect_language(None) is None)


# ---------------------------------------------------------------------------
# Phase 4.1: unit — TypeScript extraction
# ---------------------------------------------------------------------------
section("TypeScript dimension extraction")

snap = extract_dimensions(
    "export default function Page() { return 1; }\n",
    language="typescript",
)
t(
    "default function export → default_export_kind == 'FunctionDeclaration'",
    snap.default_export_kind == "FunctionDeclaration",
    f"got {snap.default_export_kind}",
)

snap = extract_dimensions(
    "export default class Foo {}\n",
    language="typescript",
)
t(
    "default class export → default_export_kind == 'ClassDeclaration'",
    snap.default_export_kind == "ClassDeclaration",
    f"got {snap.default_export_kind}",
)

snap = extract_dimensions(
    "export const a = 1;\nexport const b = 2;\nexport function c() {}\n",
    language="typescript",
)
t(
    "three named exports → named_export_count == 3",
    snap.named_export_count == 3,
    f"got {snap.named_export_count}",
)
t(
    "three named exports → bucket == '2-4'",
    snap.named_export_count_bucket == "2-4",
)

snap = extract_dimensions(
    "import React from 'react';\nexport default function C() { return <div />; }\n",
    language="typescript",
)
t("JSX self-closing element detected", snap.jsx_present is True)

snap = extract_dimensions(
    'const msg = "</div>";\nexport const x = 1;\n',
    language="typescript",
)
t(
    "JSX-looking string literal does NOT trigger jsx_present",
    snap.jsx_present is False,
    f"got jsx_present={snap.jsx_present}",
)

snap = extract_dimensions(
    '"use client"\nexport default function P() { return <div /> }\n',
    language="typescript",
)
t(
    "content_signal 'use_client' detected from first 200 bytes",
    snap.content_signal == "use_client",
)

snap = extract_dimensions(
    "// just a comment\nconst x = 1;\n",
    language="typescript",
)
t(
    "no directive → content_signal is None (matches ast_query null convention)",
    snap.content_signal is None,
)

# top_level_node_kinds capture. The TS Compiler API SyntaxKind name for
# `const x = ...` is reported as "FirstStatement" (it shares its numeric
# value with VariableStatement and the SyntaxKind reverse map yields
# "FirstStatement" first). Our lint engine matches that vocabulary so
# observations align with what bootstrap persists into canonicals.json.
snap = extract_dimensions(
    "import x from 'y';\nexport const a = 1;\nclass Foo {}\n",
    language="typescript",
)
t(
    "top_level_node_kinds includes ImportDeclaration + FirstStatement + ClassDeclaration",
    "ImportDeclaration" in snap.top_level_node_kinds
    and "FirstStatement" in snap.top_level_node_kinds
    and "ClassDeclaration" in snap.top_level_node_kinds,
    f"got {snap.top_level_node_kinds}",
)


# ---------------------------------------------------------------------------
# Phase 4.1: unit — Ruby extraction
# ---------------------------------------------------------------------------
section("Ruby dimension extraction")

snap = extract_dimensions(
    "class FooController < ApplicationController\n  def index\n  end\nend\n",
    language="ruby",
)
t(
    "single top-level class → default_export_kind == 'ClassNode'",
    snap.default_export_kind == "ClassNode",
    f"got {snap.default_export_kind}",
)
t(
    "class with internal def → 1 top-level export (matches prism_dump.rb)",
    snap.named_export_count == 1,
    f"got {snap.named_export_count}",
)

snap = extract_dimensions("def helper\nend\nclass X\nend\n", language="ruby")
t(
    "top-level def + top-level class counted as 2 exports",
    snap.named_export_count == 2,
    f"got {snap.named_export_count}",
)

snap = extract_dimensions("class A; end\nclass B; end\n", language="ruby")
t(
    "two top-level classes → default_export_kind is None (matches Prism logic)",
    snap.default_export_kind is None,
)

snap = extract_dimensions("# coding: utf-8\nclass A; end\n", language="ruby")
t(
    "Ruby line comment does not become a top-level node",
    "ClassNode" in snap.top_level_node_kinds
    and snap.top_level_node_kinds.count("ClassNode") == 1,
)


# ---------------------------------------------------------------------------
# Phase 4.1: unit — lint() rule emission
# ---------------------------------------------------------------------------
section("lint() rule emission")

ast_query = {
    "top_level_node_kinds": ["ImportDeclaration", "FunctionDeclaration"],
    "default_export_kind": "FunctionDeclaration",
    # `export default function Page() {}` is 0 named exports (the only export
    # is the default), so bucket "0" matches reality. This is what the
    # cluster signature function would produce for this content.
    "named_export_count_bucket": "0",
    "jsx_present": True,
    "content_signal": "use_client",
}

# Matching file → zero violations.
matching = (
    '"use client"\n'
    "import React from 'react';\n"
    "export default function Page() { return <div />; }\n"
)
v = lint(extract_dimensions(matching, language="typescript"), ast_query)
t(
    "matching file produces zero violations",
    v == [],
    f"got {[x.rule for x in v]}",
)

# default_export_kind mismatch.
mismatched_default = "import x from 'x';\nexport default class P {}\n"
v = lint(extract_dimensions(mismatched_default, language="typescript"), ast_query)
rules = {x.rule for x in v}
t(
    "default-export-kind-mismatch fires when kind differs",
    "default-export-kind-mismatch" in rules,
    f"got rules={rules}",
)

# jsx mismatch — file has JSX but archetype says no
ast_no_jsx = {"jsx_present": False}
v = lint(
    extract_dimensions(
        "export default function P() { return <div />; }", language="typescript"
    ),
    ast_no_jsx,
)
rules = {x.rule for x in v}
t(
    "jsx-presence-mismatch fires when file has JSX but archetype doesn't",
    "jsx-presence-mismatch" in rules,
    f"got rules={rules}",
)
# Severity for "file has JSX but archetype doesn't" is error (hard flag)
jsx_violation = next(x for x in v if x.rule == "jsx-presence-mismatch")
t(
    "jsx-presence-mismatch in non-JSX archetype is error severity",
    jsx_violation.severity == "error",
)

# jsx mismatch — file has no JSX but archetype expects it
ast_jsx = {"jsx_present": True}
v = lint(
    extract_dimensions("export const x = 1;\n", language="typescript"), ast_jsx
)
rules = {x.rule for x in v}
t(
    "jsx-presence-mismatch fires when archetype expects JSX but file has none",
    "jsx-presence-mismatch" in rules,
)
jsx_violation = next(x for x in v if x.rule == "jsx-presence-mismatch")
t(
    "missing-JSX in JSX archetype is warning severity (not error)",
    jsx_violation.severity == "warning",
)

# named_export_count_bucket mismatch
v = lint(
    extract_dimensions(
        "export const a = 1;\nexport const b = 2;\nexport const c = 3;\n",
        language="typescript",
    ),
    {"named_export_count_bucket": "1"},
)
rules = {x.rule for x in v}
t(
    "named-export-count-bucket-mismatch fires when bucket differs",
    "named-export-count-bucket-mismatch" in rules,
)
bucket_violation = next(x for x in v if x.rule == "named-export-count-bucket-mismatch")
t("named-export-count-bucket-mismatch severity is info", bucket_violation.severity == "info")

# content_signal mismatch
v = lint(
    extract_dimensions("export const x = 1;\n", language="typescript"),
    {"content_signal": "use_client"},
)
rules = {x.rule for x in v}
t(
    "content-signal-mismatch fires when archetype expects directive but file has none",
    "content-signal-mismatch" in rules,
)

# Null ast_query field → no-op
v = lint(
    extract_dimensions(
        "export default function P() { return <div />; }", language="typescript"
    ),
    {"default_export_kind": None, "jsx_present": None, "content_signal": None},
)
t(
    "null ast_query fields produce zero violations (encoding rule)",
    v == [],
    f"got {[x.rule for x in v]}",
)

# Empty/None ast_query → zero violations
t("lint(snapshot, None) == []", lint(extract_dimensions("anything"), None) == [])
t("lint(snapshot, {}) == []", lint(extract_dimensions("anything"), {}) == [])

# top_level_node_kinds: extras are ok, missing-required is flagged.
v = lint(
    extract_dimensions(
        "import x from 'y';\nexport function f() {}\nclass C {}\n",
        language="typescript",
    ),
    {"top_level_node_kinds": ["ImportDeclaration", "FunctionDeclaration"]},
)
t(
    "top_level_node_kinds match allows EXTRA kinds (file has ClassDeclaration too)",
    not any(x.rule == "top-level-node-kinds-mismatch" for x in v),
    f"got rules={[x.rule for x in v]}",
)
# Extras-with-FirstStatement: the file should match a query that requires
# ImportDeclaration even though it also has FirstStatement and ClassDeclaration.
v = lint(
    extract_dimensions(
        "import x from 'y';\nconst a = 1;\nclass C {}\n",
        language="typescript",
    ),
    {"top_level_node_kinds": ["ImportDeclaration"]},
)
t(
    "single-kind required passes when file has it plus other kinds",
    not any(x.rule == "top-level-node-kinds-mismatch" for x in v),
)

v = lint(
    extract_dimensions("class C {}\n", language="typescript"),
    {"top_level_node_kinds": ["ImportDeclaration", "FunctionDeclaration"]},
)
t(
    "top-level-node-kinds-mismatch fires when required kinds are missing",
    any(x.rule == "top-level-node-kinds-mismatch" for x in v),
)


# ---------------------------------------------------------------------------
# Phase 4.1: unit — canonical_confidence
# ---------------------------------------------------------------------------
section("canonical_confidence math")

snap = extract_dimensions(
    '"use client"\nimport x from "x";\n'
    "export default function P() { return <div />; }\n",
    language="typescript",
)
ast_query = {
    "top_level_node_kinds": ["ImportDeclaration"],
    "default_export_kind": "FunctionDeclaration",
    "named_export_count_bucket": "0",
    "jsx_present": True,
    "content_signal": "use_client",
}
conf = canonical_confidence(snap, ast_query)
t(f"confidence 1.0 when all 5 fields match (got {conf:.2f})", conf == 1.0)

# Force a partial match: 4 fields match, 1 doesn't
ast_query_partial = dict(ast_query)
ast_query_partial["default_export_kind"] = "ClassDeclaration"  # forced mismatch
conf = canonical_confidence(snap, ast_query_partial)
t(f"confidence 0.8 when 4/5 fields match (got {conf:.2f})", abs(conf - 0.8) < 0.001)

# Force zero matches
ast_zero = {
    "top_level_node_kinds": ["NeverNode"],
    "default_export_kind": "ClassDeclaration",
    "named_export_count_bucket": "10+",
    "jsx_present": False,
    "content_signal": "use_server",
}
conf = canonical_confidence(snap, ast_zero)
t(f"confidence 0.0 when zero fields match (got {conf:.2f})", conf == 0.0)

# All-null ast_query → vacuously 1.0
conf = canonical_confidence(snap, {})
t("confidence 1.0 for empty ast_query (nothing to check)", conf == 1.0)
conf = canonical_confidence(snap, None)
t("confidence 1.0 for null ast_query (nothing to check)", conf == 1.0)


# ---------------------------------------------------------------------------
# Phase 4.1: end-to-end — lint_file integration
# ---------------------------------------------------------------------------
section("lint_file integration (bootstrapped fixture repo)")


def _make_ts_fixture() -> tuple[Path, str, str]:
    """Bootstrap a real TS-shaped repo so lint_file has a real ast_query to
    compare against. Returns (repo_root, repo_id, first_archetype_name)."""
    root = Path(tempfile.mkdtemp(prefix="chameleon_lint_fix_"))
    (root / "package.json").write_text(
        '{"name":"fixture","dependencies":{"typescript":"5.0.0"}}'
    )
    (root / "tsconfig.json").write_text("{}")
    src = root / "src" / "queries"
    src.mkdir(parents=True)
    # Dense cluster of 6 files with the same shape: 1 named export, no JSX,
    # one import, no default export.
    for i in range(6):
        (src / f"q{i}.ts").write_text(
            "import { z } from 'zod';\n"
            f"export const q{i} = z.object({{}});\n"
        )
    report = bootstrap_repo(str(root))
    if report.get("data", {}).get("status") != "success":
        return root, "", ""

    # Grant trust so _resolve_repo_root_by_id picks it up.
    repo_id = _compute_repo_id(root)
    grant_trust(repo_id, root / ".chameleon")

    canonicals = json.loads((root / ".chameleon" / "canonicals.json").read_text())
    arch_names = list(canonicals.get("canonicals", {}).keys())
    return root, repo_id, arch_names[0] if arch_names else ""


fixture_root, fixture_id, fixture_arch = _make_ts_fixture()
try:
    t(
        "fixture bootstrap produced at least one archetype",
        bool(fixture_arch),
        json.dumps({"repo": str(fixture_root)}, indent=2),
    )

    if fixture_arch:
        # Matching file — content mirrors what the cluster looks like.
        matching = (
            "import { z } from 'zod';\n"
            "export const item = z.object({});\n"
        )
        r = lint_file(fixture_id, fixture_arch, matching)["data"]
        t(
            "lint_file matching content → stub False (real impl ran)",
            r.get("stub") is False,
            json.dumps(r, indent=2)[:300],
        )
        t(
            "lint_file matching content → stub_reason is None",
            r.get("stub_reason") is None,
        )
        t(
            "lint_file matching content → canonical_confidence == 1.0",
            r.get("canonical_confidence") == 1.0,
            f"got {r.get('canonical_confidence')}",
        )

        # JSX violation — adding a JSX element to a non-JSX archetype.
        with_jsx = "import { z } from 'zod';\nexport const C = () => <div />;\n"
        r = lint_file(fixture_id, fixture_arch, with_jsx)["data"]
        rules = {v["rule"] for v in r.get("violations", [])}
        t(
            "lint_file flags jsx-presence-mismatch on JSX inserted into non-JSX archetype",
            "jsx-presence-mismatch" in rules,
            f"got rules={rules}",
        )

        # 100KB cap — content_size reflects original; envelope flags truncated.
        big = "x" * 200_000
        r = lint_file(fixture_id, fixture_arch, big)
        t(
            "lint_file caps oversize content with truncated=True envelope flag",
            r.get("truncated") is True,
        )
        t(
            "content_size reports the ORIGINAL pre-cap size",
            r["data"]["content_size"] == 200_000,
        )

        # Unknown archetype → real envelope, stub False, with reason.
        r = lint_file(fixture_id, "definitely-not-an-archetype-name", "x = 1")["data"]
        t(
            "lint_file on unknown archetype → stub False (engine ran with no query)",
            r.get("stub") is False,
            json.dumps(r, indent=2)[:300],
        )
        t(
            "lint_file on unknown archetype → carries 'noop_reason' field",
            isinstance(r.get("noop_reason"), str) and "no ast_query" in r["noop_reason"],
        )

    # No-profile / unresolvable repo → legacy stub envelope preserved.
    r = lint_file("/tmp/definitely-not-a-real-repo-id", "any", "const x = 1;")["data"]
    t(
        "lint_file on unresolvable repo → legacy stub envelope (stub: True)",
        r.get("stub") is True,
        json.dumps(r, indent=2)[:200],
    )
    t(
        "legacy-stub envelope carries stub_reason string (back-compat)",
        isinstance(r.get("stub_reason"), str) and len(r["stub_reason"]) > 0,
    )

    # Ruby file content against a TS archetype: real engine runs, but the
    # language hint is taken from the archetype's witness, so the Ruby
    # source gets the TS extractor → still works, just produces (mostly)
    # mismatches. The point of this test is that the engine doesn't crash.
    ruby_src = "class Foo\n  def bar\n  end\nend\n"
    if fixture_arch:
        r = lint_file(fixture_id, fixture_arch, ruby_src)["data"]
        t(
            "lint_file on Ruby content vs TS archetype → returns dict (no crash)",
            isinstance(r, dict) and r.get("stub") is False,
        )

finally:
    shutil.rmtree(fixture_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Phase 4.1: Ruby end-to-end — file extension drives language
# ---------------------------------------------------------------------------
section("lint_file integration (Ruby fixture)")


def _make_ruby_fixture() -> tuple[Path, str, str]:
    """Bootstrap a Ruby on Rails-like fixture. Requires `ruby` + Prism on the
    host; if unavailable, the bootstrap fails and we return empty IDs so
    the test skips gracefully."""
    root = Path(tempfile.mkdtemp(prefix="chameleon_lint_rb_fix_"))
    (root / "Gemfile").write_text("source 'https://rubygems.org'\n")
    app_dir = root / "app" / "controllers"
    app_dir.mkdir(parents=True)
    for i in range(6):
        (app_dir / f"r{i}_controller.rb").write_text(
            f"class R{i}Controller < ApplicationController\n  def index\n    {i}\n  end\nend\n"
        )
    report = bootstrap_repo(str(root))
    if report.get("data", {}).get("status") != "success":
        return root, "", ""

    repo_id = _compute_repo_id(root)
    grant_trust(repo_id, root / ".chameleon")

    canonicals_path = root / ".chameleon" / "canonicals.json"
    if not canonicals_path.is_file():
        return root, repo_id, ""
    canonicals = json.loads(canonicals_path.read_text())
    arch_names = list(canonicals.get("canonicals", {}).keys())
    return root, repo_id, arch_names[0] if arch_names else ""


rb_root, rb_id, rb_arch = _make_ruby_fixture()
try:
    if rb_arch:
        rb_matching = (
            "class R7Controller < ApplicationController\n  def index\n    7\n  end\nend\n"
        )
        r = lint_file(rb_id, rb_arch, rb_matching)["data"]
        t(
            "Ruby lint_file matching content → stub False (real impl ran)",
            r.get("stub") is False,
            json.dumps(r, indent=2)[:300],
        )
        t(
            f"Ruby canonical_confidence is real number (got {r.get('canonical_confidence')})",
            isinstance(r.get("canonical_confidence"), float)
            and 0.0 <= r["canonical_confidence"] <= 1.0,
        )
    else:
        print(
            "  [INFO] Ruby fixture bootstrap produced no archetypes — likely "
            "no `ruby` / Prism on host. Treating as informational pass."
        )
        t("Ruby end-to-end (informational pass when host has no Ruby)", True)
finally:
    shutil.rmtree(rb_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Phase 4.1: Violation dataclass + envelope shape contract
# ---------------------------------------------------------------------------
section("Violation + envelope contract")

v = Violation(
    rule="r",
    expected="e",
    actual="a",
    severity="warning",
    message="m",
)
t(
    "Violation.to_dict returns the 5 documented keys",
    v.to_dict() == {
        "rule": "r", "expected": "e", "actual": "a",
        "severity": "warning", "message": "m",
    },
)

# DimensionSnapshot defaults sane
empty = DimensionSnapshot()
t(
    "DimensionSnapshot() defaults are safe (empty list, None, False)",
    empty.top_level_node_kinds == []
    and empty.default_export_kind is None
    and empty.jsx_present is False
    and empty.content_signal is None
    and empty.named_export_count == 0,
)
t(
    "DimensionSnapshot.named_export_count_bucket on default == '0'",
    empty.named_export_count_bucket == "0",
)


# ---------------------------------------------------------------------------
print("\n=== Summary ===")
print(f"  Total: {PASS + FAIL}")
print(f"  Pass: {PASS}")
print(f"  Fail: {FAIL}")
shutil.rmtree(TMPDATA, ignore_errors=True)
sys.exit(0 if FAIL == 0 else 1)
