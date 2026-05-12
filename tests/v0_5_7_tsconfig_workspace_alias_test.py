"""BUG-NEW-012 (v0.5.7): tsconfig extends resolves workspace package alias.

Two bugs together:
  1. _strip_jsonc_comments ate URL string literals (e.g. "https://...") because
     it naively split on `//` outside string-aware scanning. tsconfigs with
     a $schema URL silently failed to parse.
  2. The bare-specifier resolver only looked under node_modules/. pnpm /
     yarn-workspace monorepos link config packages from
     packages/<name>/ — never under node_modules in fresh checkouts.

Both fixed in v0.5.7-redo. This test exercises both together.
"""

import json
import sys
import tempfile
from pathlib import Path

from chameleon_mcp.bootstrap.tool_config import (
    _strip_jsonc_comments,
    read_tool_configs,
)

PASS, FAIL = [], []


def t(name, condition, info=""):
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' - ' + info) if info else ''}")


def section(title):
    print(f"\n=== {title} ===")


section("JSONC preserves URLs in string literals")
text = '{"$schema": "https://json.schemastore.org/tsconfig", "compilerOptions": {}}'
stripped = _strip_jsonc_comments(text)
t("URL not corrupted by // strip", "https://json.schemastore.org/tsconfig" in stripped,
  f"got {stripped!r}")
t("parses cleanly", json.loads(stripped).get("$schema").startswith("https://"))


section("JSONC still strips end-of-line // comments")
text = '{"k": 1 // a comment\n}'
stripped = _strip_jsonc_comments(text)
parsed = json.loads(stripped)
t("// comment stripped", parsed.get("k") == 1, f"got {parsed!r}")


section("JSONC still strips /* */ block comments")
text = '{"k": /* mid */ 1}'
stripped = _strip_jsonc_comments(text)
parsed = json.loads(stripped)
t("block comment stripped", parsed.get("k") == 1, f"got {parsed!r}")


section("Workspace alias resolves in pnpm-style monorepo")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "monorepo"
    root.mkdir()
    (root / "pnpm-workspace.yaml").write_text("packages:\n  - 'packages/*'\n  - 'apps/*'\n")
    (root / "package.json").write_text("{}")

    # Shared tsconfig package
    cfg_pkg = root / "packages" / "tsconfig"
    cfg_pkg.mkdir(parents=True)
    (cfg_pkg / "package.json").write_text('{"name": "@org/tsconfig"}')
    (cfg_pkg / "base.json").write_text(
        '{"compilerOptions": {"strict": true, "target": "esnext", "jsx": "react-jsx"}}'
    )

    # Consumer workspace that extends @org/tsconfig/base.json
    consumer = root / "packages" / "ui"
    consumer.mkdir(parents=True)
    (consumer / "package.json").write_text(
        '{"name": "@org/ui", "dependencies": {"typescript": "5"}}'
    )
    (consumer / "tsconfig.json").write_text(
        '{"$schema": "https://json.schemastore.org/tsconfig", '
        '"extends": "@org/tsconfig/base.json", '
        '"compilerOptions": {}}'
    )
    (consumer / "src").mkdir()
    (consumer / "src" / "foo.ts").write_text("export const x = 1;")

    r = read_tool_configs(consumer)
    t("no parse warnings", not r.parse_warnings, f"got {r.parse_warnings}")
    chain = r.tsconfig_extends_chain
    t("extends_chain includes shared base.json",
      any("base.json" in s for s in chain),
      f"got {chain}")
    co = (r.tsconfig or {}).get("compilerOptions", {})
    t("strict resolved from shared base", co.get("strict") is True,
      f"got {co.get('strict')}")
    t("jsx resolved from shared base", co.get("jsx") == "react-jsx",
      f"got {co.get('jsx')}")


print(f"\n=== Summary: {len(PASS)} pass, {len(FAIL)} fail ===")
if FAIL:
    for name, info in FAIL:
        print(f"  FAIL: {name} - {info}")
    sys.exit(1)
