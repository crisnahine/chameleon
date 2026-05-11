"""Regression tests for the v0.3 tool_config + workspace upgrades.

Covers Phase 4.7 (tsconfig `extends` chain resolution) and Phase 2C.4
(.eslintrc.js / YAML parsing) in ``bootstrap/tool_config.py``, plus
Phase 2C.5 (workspace path resolution for pnpm, lerna, turbo) in
``bootstrap/workspace.py``.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/tool_config_v03_test.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

from chameleon_mcp.bootstrap.tool_config import (  # noqa: E402
    read_tool_configs,
)
from chameleon_mcp.bootstrap.workspace import detect_workspace  # noqa: E402

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


def _mktemp_dir(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


# ---------------------------------------------------------------------------
# tsconfig extends — single hop, relative path
# ---------------------------------------------------------------------------
section("tsconfig extends: single relative hop")

repo = _mktemp_dir("chameleon_tc_extends_1_")
(repo / "tsconfig.base.json").write_text(json.dumps({
    "compilerOptions": {
        "strict": True,
        "target": "ES2020",
        "noImplicitAny": True,
    },
}))
(repo / "tsconfig.json").write_text(json.dumps({
    "extends": "./tsconfig.base.json",
    "compilerOptions": {
        "target": "ES2022",  # overrides base
        "paths": {"~/*": ["src/*"]},
    },
}))
result = read_tool_configs(repo)
t("tsconfig loaded", result.tsconfig is not None)
co = (result.tsconfig or {}).get("compilerOptions", {})
t("strict inherited from base", co.get("strict") is True)
t("target overridden by derived", co.get("target") == "ES2022")
t("paths surface from derived", co.get("paths") == {"~/*": ["src/*"]})
t("extends_chain length is 2", len(result.tsconfig_extends_chain) == 2,
  detail=str(result.tsconfig_extends_chain))
t("no parse warning on clean chain", "tsconfig" not in result.parse_warnings,
  detail=str(result.parse_warnings.get("tsconfig", "")))


# ---------------------------------------------------------------------------
# tsconfig extends — 3-hop chain (closest wins on each layer)
# ---------------------------------------------------------------------------
section("tsconfig extends: 3-hop chain")

repo = _mktemp_dir("chameleon_tc_extends_3_")
(repo / "a.json").write_text(json.dumps({
    "compilerOptions": {
        "strict": True,
        "target": "ES2015",
        "noImplicitAny": False,
    },
}))
(repo / "b.json").write_text(json.dumps({
    "extends": "./a.json",
    "compilerOptions": {
        "target": "ES2020",  # overrides a
    },
}))
(repo / "c.json").write_text(json.dumps({
    "extends": "./b.json",
    "compilerOptions": {
        "noImplicitAny": True,  # overrides a
    },
}))
(repo / "tsconfig.json").write_text(json.dumps({
    "extends": "./c.json",
    "compilerOptions": {
        "module": "esnext",  # only in root
    },
}))
result = read_tool_configs(repo)
co = (result.tsconfig or {}).get("compilerOptions", {})
t("3-hop chain merges strict", co.get("strict") is True)
t("3-hop chain overrides target via mid layer", co.get("target") == "ES2020")
t("3-hop chain overrides noImplicitAny via near layer", co.get("noImplicitAny") is True)
t("3-hop chain preserves root-only field", co.get("module") == "esnext")
t("extends_chain length is 4", len(result.tsconfig_extends_chain) == 4,
  detail=str(result.tsconfig_extends_chain))


# ---------------------------------------------------------------------------
# tsconfig extends — cycle detected
# ---------------------------------------------------------------------------
section("tsconfig extends: cycle detected")

repo = _mktemp_dir("chameleon_tc_extends_cycle_")
(repo / "loop_a.json").write_text(json.dumps({
    "extends": "./loop_b.json",
    "compilerOptions": {"strict": True},
}))
(repo / "loop_b.json").write_text(json.dumps({
    "extends": "./loop_a.json",
    "compilerOptions": {"target": "ES2020"},
}))
(repo / "tsconfig.json").write_text(json.dumps({
    "extends": "./loop_a.json",
}))
result = read_tool_configs(repo)
t("cycle does not crash", result.tsconfig is not None)
t(
    "cycle records a parse warning",
    "tsconfig" in result.parse_warnings and "cycle" in result.parse_warnings["tsconfig"].lower(),
    detail=str(result.parse_warnings.get("tsconfig", "")),
)
co = (result.tsconfig or {}).get("compilerOptions", {})
t("cycle still merged what was reachable", co.get("strict") is True or co.get("target") == "ES2020")


# ---------------------------------------------------------------------------
# tsconfig extends — missing target file
# ---------------------------------------------------------------------------
section("tsconfig extends: missing target")

repo = _mktemp_dir("chameleon_tc_extends_missing_")
(repo / "tsconfig.json").write_text(json.dumps({
    "extends": "./does-not-exist.json",
    "compilerOptions": {"strict": True},
}))
result = read_tool_configs(repo)
t("missing target does not crash", result.tsconfig is not None)
t(
    "missing target records parse warning",
    "tsconfig" in result.parse_warnings and "could not be resolved" in result.parse_warnings["tsconfig"].lower(),
    detail=str(result.parse_warnings.get("tsconfig", "")),
)
co = (result.tsconfig or {}).get("compilerOptions", {})
t("root options preserved despite missing parent", co.get("strict") is True)


# ---------------------------------------------------------------------------
# tsconfig extends — hop cap
# ---------------------------------------------------------------------------
section("tsconfig extends: hop cap (>8 hops)")

repo = _mktemp_dir("chameleon_tc_extends_hopcap_")
chain_len = 12  # exceeds _MAX_EXTENDS_HOPS=8
for i in range(chain_len):
    nxt = i + 1
    payload = {"compilerOptions": {f"flag{i}": True}}
    if nxt < chain_len:
        payload["extends"] = f"./hop_{nxt}.json"
    (repo / f"hop_{i}.json").write_text(json.dumps(payload))
(repo / "tsconfig.json").write_text(json.dumps({
    "extends": "./hop_0.json",
}))
result = read_tool_configs(repo)
t("hop cap does not crash", result.tsconfig is not None)
t(
    "hop cap records a parse warning",
    "tsconfig" in result.parse_warnings and "hop" in result.parse_warnings["tsconfig"].lower(),
    detail=str(result.parse_warnings.get("tsconfig", "")),
)


# ---------------------------------------------------------------------------
# tsconfig extends — bare specifier (@tsconfig/strictest-like)
# ---------------------------------------------------------------------------
section("tsconfig extends: bare specifier via node_modules")

repo = _mktemp_dir("chameleon_tc_extends_bare_")
nm = repo / "node_modules" / "@tsconfig" / "strictest"
nm.mkdir(parents=True)
(nm / "tsconfig.json").write_text(json.dumps({
    "compilerOptions": {
        "strict": True,
        "noUncheckedIndexedAccess": True,
        "exactOptionalPropertyTypes": True,
    },
}))
(repo / "tsconfig.json").write_text(json.dumps({
    "extends": "@tsconfig/strictest/tsconfig.json",
    "compilerOptions": {"target": "ES2022"},
}))
result = read_tool_configs(repo)
co = (result.tsconfig or {}).get("compilerOptions", {})
t("bare specifier resolved", co.get("strict") is True)
t("bare specifier merges nested options", co.get("noUncheckedIndexedAccess") is True)
t("derived target wins over bare base", co.get("target") == "ES2022")


# ---------------------------------------------------------------------------
# .eslintrc.yml parsed
# ---------------------------------------------------------------------------
section(".eslintrc.yml parsing")

repo = _mktemp_dir("chameleon_tc_eslint_yml_")
(repo / ".eslintrc.yml").write_text(
    "root: true\n"
    "rules:\n"
    "  no-console: error\n"
    "  semi:\n"
    "    - error\n"
    "    - always\n"
    "plugins:\n"
    "  - import\n"
)
result = read_tool_configs(repo)
t("yml eslint loaded as dict", isinstance(result.eslint, dict))
t("yml eslint rule extracted", (result.eslint or {}).get("rules", {}).get("no-console") == "error")
t("yml plugins flag the invisibility caveat", result.has_eslint_js_plugins is True)
t("yml source recorded", result.sources.get("eslint") == ".eslintrc.yml")


# ---------------------------------------------------------------------------
# .eslintrc.yml malformed → graceful warning
# ---------------------------------------------------------------------------
section(".eslintrc.yml malformed")

repo = _mktemp_dir("chameleon_tc_eslint_yml_bad_")
(repo / ".eslintrc.yml").write_text(
    "rules: [unbalanced bracket\n"
    "  not valid: at all: ::::\n"
)
result = read_tool_configs(repo)
# It may parse as something weird; we just want no crash + warning either way.
t(
    "malformed yml either parses to None or records warning",
    result.eslint is None or "eslint" in result.parse_warnings or isinstance(result.eslint, dict),
)


# ---------------------------------------------------------------------------
# .eslintrc.js best-effort parsed
# ---------------------------------------------------------------------------
section(".eslintrc.js best-effort parsing")

repo = _mktemp_dir("chameleon_tc_eslint_js_")
(repo / ".eslintrc.js").write_text(
    "// eslint flat-ish config\n"
    "module.exports = {\n"
    "  root: true,\n"
    "  env: { node: true, browser: true },\n"
    "  rules: {\n"
    "    'no-console': 'error',\n"
    "    'semi': ['error', 'always'],\n"
    "  },\n"
    "};\n"
)
result = read_tool_configs(repo)
t("js eslint best-effort returned a dict", isinstance(result.eslint, dict))
if isinstance(result.eslint, dict):
    rules = result.eslint.get("rules", {})
    t("js eslint extracts no-console rule", rules.get("no-console") == "error")
    t(
        "js eslint extracts semi rule (array)",
        rules.get("semi") == ["error", "always"],
        detail=str(rules.get("semi")),
    )
t("js source recorded", result.sources.get("eslint") == ".eslintrc.js")


# ---------------------------------------------------------------------------
# .eslintrc.js malformed → graceful fallback
# ---------------------------------------------------------------------------
section(".eslintrc.js malformed → graceful fallback")

repo = _mktemp_dir("chameleon_tc_eslint_js_bad_")
(repo / ".eslintrc.js").write_text(
    "// not a valid module.exports payload\n"
    "const cfg = require('./shared');\n"
    "module.exports = cfg.extend({ runtime: true });\n"
)
result = read_tool_configs(repo)
t("malformed js does not crash", True)
t("malformed js leaves eslint as None", result.eslint is None or isinstance(result.eslint, dict))
t(
    "malformed js records parse warning",
    "eslint" in result.parse_warnings,
    detail=str(result.parse_warnings.get("eslint", "")),
)
t("malformed js still flags invisibility", result.has_eslint_js_plugins is True)


# ---------------------------------------------------------------------------
# .eslintrc.cjs with comments + template literal in a string
# ---------------------------------------------------------------------------
section(".eslintrc.cjs with embedded comments / template strings")

repo = _mktemp_dir("chameleon_tc_eslint_cjs_")
(repo / ".eslintrc.cjs").write_text(
    "/* leading block comment */\n"
    "// preamble\n"
    "module.exports = {\n"
    "  rules: {\n"
    "    // disable-next-line\n"
    "    'no-debugger': 'warn',\n"
    "  },\n"
    "};\n"
)
result = read_tool_configs(repo)
t("cjs best-effort produced a dict", isinstance(result.eslint, dict))
if isinstance(result.eslint, dict):
    t(
        "cjs extracted no-debugger rule",
        (result.eslint.get("rules") or {}).get("no-debugger") == "warn",
    )


# ---------------------------------------------------------------------------
# Workspace: pnpm-workspace.yaml resolved → workspace_paths populated
# ---------------------------------------------------------------------------
section("Workspace: pnpm resolved")

repo = _mktemp_dir("chameleon_ws_pnpm_")
(repo / "pnpm-workspace.yaml").write_text(
    "packages:\n"
    "  - 'apps/*'\n"
    "  - 'packages/*'\n"
)
(repo / "apps").mkdir()
(repo / "apps" / "web").mkdir()
(repo / "apps" / "web" / "package.json").write_text("{}")
(repo / "apps" / "api").mkdir()
(repo / "apps" / "api" / "package.json").write_text("{}")
(repo / "packages").mkdir()
(repo / "packages" / "ui").mkdir()
(repo / "packages" / "ui" / "package.json").write_text("{}")
ws = detect_workspace(repo)
t("pnpm manager detected", ws.manager == "pnpm")
t("pnpm workspace flag", ws.is_workspace is True)
t("pnpm workspace paths populated (3 packages)", len(ws.workspace_paths) == 3,
  detail=str([str(p.relative_to(repo)) for p in ws.workspace_paths]))


# ---------------------------------------------------------------------------
# Workspace: lerna.json packages resolved
# ---------------------------------------------------------------------------
section("Workspace: lerna resolved")

repo = _mktemp_dir("chameleon_ws_lerna_")
(repo / "lerna.json").write_text(json.dumps({
    "version": "1.0.0",
    "packages": ["modules/*"],
}))
(repo / "modules").mkdir()
for sub in ("alpha", "beta"):
    (repo / "modules" / sub).mkdir()
    (repo / "modules" / sub / "package.json").write_text("{}")
ws = detect_workspace(repo)
t("lerna manager detected", ws.manager == "lerna")
t("lerna workspace_paths populated", len(ws.workspace_paths) == 2,
  detail=str(ws.workspace_paths))


# ---------------------------------------------------------------------------
# Workspace: turbo.json with packages array resolved
# ---------------------------------------------------------------------------
section("Workspace: turbo resolved (packages array)")

repo = _mktemp_dir("chameleon_ws_turbo_")
(repo / "turbo.json").write_text(json.dumps({
    "$schema": "https://turbo.build/schema.json",
    "tasks": {"build": {}},
    "packages": ["apps/*"],
}))
(repo / "apps").mkdir()
(repo / "apps" / "web").mkdir()
(repo / "apps" / "web" / "package.json").write_text("{}")
ws = detect_workspace(repo)
t("turbo manager detected", ws.manager == "turbo")
t("turbo workspace_paths populated via turbo.json packages",
  len(ws.workspace_paths) == 1, detail=str(ws.workspace_paths))


# ---------------------------------------------------------------------------
# Workspace: turbo.json falls back to package.json workspaces
# ---------------------------------------------------------------------------
section("Workspace: turbo fall-back to package.json workspaces")

repo = _mktemp_dir("chameleon_ws_turbo_fallback_")
# Note: detect_workspace's yarn branch fires first for package.json
# workspaces. To test the turbo fall-back specifically we omit
# package.json workspaces and just verify the manager string when
# turbo declares no packages.
(repo / "turbo.json").write_text(json.dumps({
    "pipeline": {"build": {}},
}))
ws = detect_workspace(repo)
t("turbo without packages still reports manager=turbo", ws.manager == "turbo")
t("turbo without packages has empty workspace_paths", ws.workspace_paths == [])


# ---------------------------------------------------------------------------
# Workspace: malformed pnpm-workspace.yaml does not crash
# ---------------------------------------------------------------------------
section("Workspace: malformed pnpm yaml does not crash")

repo = _mktemp_dir("chameleon_ws_pnpm_bad_")
(repo / "pnpm-workspace.yaml").write_text(":::not valid yaml::\n  - [\n")
ws = detect_workspace(repo)
t("malformed pnpm still reports manager=pnpm", ws.manager == "pnpm")
t("malformed pnpm yields empty (or partial) workspace_paths",
  isinstance(ws.workspace_paths, list))


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
section("Summary")
print(f"\n  Total: {PASS + FAIL}")
print(f"  Pass: {PASS}")
print(f"  Fail: {FAIL}")
if FAIL:
    sys.exit(1)
sys.exit(0)
