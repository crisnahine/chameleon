"""Verification of BUG-003 (continued): ad-hoc monorepos surface workspace tool configs.

Pre-v0.5.7 bootstrapping bulletproof-react read root tool_configs only.
The repo has no root .eslintrc.cjs / .prettierrc / tsconfig.json — each
apps/*/ workspace has its own. Bootstrap returned rules_extracted: 0,
and get_pattern_context for any source file returned rules: [].

Fix: when workspace_roots is non-empty AND root has no own tool configs,
adopt the first workspace's configs as repo-wide (with source paths
prefixed by the workspace path so the user knows the origin).
"""

import json
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


from chameleon_mcp.tools import bootstrap_repo, get_pattern_context

section("Ad-hoc monorepo: per-workspace .eslintrc.cjs is adopted as repo-wide")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "monorepo"
    root.mkdir()
    # Root has only scripts (no TS deps, no tsconfig.json) — ad-hoc layout.
    (root / "package.json").write_text(
        '{"name":"monorepo","private":true,"scripts":{"build":"echo"}}'
    )
    # 3 apps each with its own tooling.
    for app in ("alpha", "beta", "gamma"):
        a = root / "apps" / app
        a.mkdir(parents=True)
        (a / "package.json").write_text(
            '{"name":"' + app + '","dependencies":{"typescript":"5","eslint":"8","react":"18"}}'
        )
        (a / "tsconfig.json").write_text('{"compilerOptions":{"strict":true}}')
        (a / ".eslintrc.cjs").write_text(
            "module.exports = { extends: ['eslint:recommended'], rules: { 'no-console': 'warn' } };"
        )
        (a / ".prettierrc").write_text('{"singleQuote":true,"semi":false}')
        (a / "src").mkdir()
        for i in range(4):
            (a / "src" / f"comp{i}.tsx").write_text(f"export const X{i} = () => null;")

    resp = bootstrap_repo(str(root))
    t("bootstrap succeeded", resp["data"]["status"] == "success",
      f"got status={resp['data']['status']} error={resp['data'].get('error')}")
    t("rules_extracted > 0", resp["data"].get("rules_extracted", 0) > 0,
      f"got rules_extracted={resp['data'].get('rules_extracted')}")

    rules_path = root / ".chameleon" / "rules.json"
    if rules_path.is_file():
        rj = json.loads(rules_path.read_text())
        rule_keys = list(rj.get("rules", {}).keys())
        t("rules.json has eslint", "eslint" in rule_keys, f"keys={rule_keys}")
        t("rules.json has prettier (formatting)", "formatting" in rule_keys, f"keys={rule_keys}")
        t("rules.json has typescript", "typescript" in rule_keys, f"keys={rule_keys}")

        eslint_rule = rj.get("rules", {}).get("eslint", {})
        src = eslint_rule.get("source", "")
        t("eslint source is workspace-prefixed",
          "apps/" in src and ".eslintrc.cjs" in src,
          f"source={src}")

    # get_pattern_context for a workspace file should now return rules
    ctx = get_pattern_context(str(root / "apps" / "alpha" / "src" / "comp0.tsx"))
    t("get_pattern_context returns rules for a workspace file",
      len(ctx["data"].get("rules", [])) > 0,
      f"got {len(ctx['data'].get('rules', []))} rules")

print(f"\n=== Summary: {len(PASS)} pass, {len(FAIL)} fail ===")
if FAIL:
    for name, info in FAIL:
        print(f"  FAIL: {name} — {info}")
    sys.exit(1)
