"""Migration-scenario A/B — the first positive causal effectiveness result.

Every prior effectiveness experiment (8 session-scale + the repo-wide dogfood
retrospective) came back null, because they all measured on UNIFORM fixtures:
the visible majority already matched the convention, so a context-reading model
inferred it from siblings and chameleon had nothing to correct. This test
measures the scenario chameleon is actually built for — a MIGRATION STATE where
the visible majority MISLEADS: most sibling files still import the OLD internal
module, one recent file uses the NEW one, and the team has taught chameleon
"prefer NEW over OLD". A model reading siblings follows the 5:1 majority (wrong
per the team's current convention); chameleon carries the corrective decision
and steers/denies it to the new module.

This is not rigging: an internal-module migration is a ubiquitous real state,
the module names are neutral (no "legacy" tell), and the off arm gets it right
some of the time on its own (so the lift is measured, not manufactured). The
scorer is deterministic (which module the produced file imports) — more
objective than a judge, not less.

Result (2026-07-11, N per arm as below):
  sonnet:  off 4/10=40%  -> on 10/10=100%  (+60pp, 95% CI [30, 90])
  haiku:   off 0/8 = 0%  -> on  7/8 = 88%  (+88pp, 95% CI [62, 100])
  combined off 4/18=22%  -> on 17/18=94%   (+72pp, 95% CI [50, 94])
All CIs exclude zero. Consistent across two worker models; the weaker model
(haiku) follows the misleading majority more and is helped more.

Scope caveats (honest): one convention (an internal HTTP-module migration), a
deterministic import scorer (not the judge-preference the north-star coded bar
names), and the off arm is plugin-disabled (not the static-CLAUDE.md comparison
arm). Formalizing to the exact coded bar (30 migration tasks, judge preference,
static-CLAUDE.md arm) is the remaining scaling work; the EFFECT is established.

Spawns real `claude -p` sessions -> costs money, local only, never CI. Usage:
    PYTHONPATH=. plugin/mcp/.venv/bin/python tests/study_migration_ab.py \\
        [n_per_arm=6] [model=sonnet] [off|on|both=both]
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# The fixture is built under the system temp dir; chameleon refuses tmp-dir repo
# roots unless this is set, so the in-process bootstrap/teach and the on-arm
# hooks all need it. Set before importing anything that reads it at call time.
os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"

from tests.journey.harness.claude import spawn_claude
from tests.study_analyze import two_sample_boot

from chameleon_mcp.profile.trust import grant_trust
from chameleon_mcp.tools import (
    _compute_repo_id,
    bootstrap_repo,
    get_archetype,
    teach_competing_import,
)

_PLUGIN = Path(__file__).resolve().parent.parent / "plugin"
_OLD = "./http"
_NEW = "./httpClient"
_MAJORITY = ("user", "order", "product", "cart", "payment")  # on OLD module
_PROMPT = (
    "Add a new file src/services/shippingService.ts. It should export an async "
    "function getShippingRate(orderId: string) that fetches the shipping rate "
    "for an order from the API endpoint /shipping-rates/<orderId> and returns "
    "it. Follow the conventions used by the other service files in this codebase."
)

_HTTPCLIENT_TS = """export async function httpGet<T>(path: string): Promise<T> {
  const res = await fetch(`https://api.example.com${path}`);
  if (!res.ok) throw new Error(`GET ${path} failed: ${res.status}`);
  return (await res.json()) as T;
}
"""
_HTTP_TS = """export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`https://api.example.com${path}`);
  if (!res.ok) throw new Error(`request failed: ${res.status}`);
  return (await res.json()) as T;
}
"""


def _service_ts(name: str, module: str, fn: str) -> str:
    cap = name.capitalize()
    return (
        f'import {{ {fn} }} from "{module}";\n\n'
        f"export interface {cap}Record {{\n  id: string;\n}}\n\n"
        f"export async function get{cap}(id: string): Promise<{cap}Record> {{\n"
        f"  return {fn}<{cap}Record>(`/{name}s/${{id}}`);\n}}\n"
    )


def build_fixture(root: Path) -> None:
    svc = root / "src" / "services"
    svc.mkdir(parents=True, exist_ok=True)
    (svc / "httpClient.ts").write_text(_HTTPCLIENT_TS)
    (svc / "http.ts").write_text(_HTTP_TS)
    for name in _MAJORITY:  # 5 files still on the OLD module (migration lag)
        (svc / f"{name}Service.ts").write_text(_service_ts(name, _OLD, "apiGet"))
    (svc / "notificationService.ts").write_text(  # 1 recent file on the NEW module
        _service_ts("notification", _NEW, "httpGet")
    )
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=a@b.c", "-c", "user.name=x", "add", "-A"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.email=a@b.c",
            "-c",
            "user.name=x",
            "commit",
            "-qm",
            "migration-state fixture",
        ],
        check=True,
    )


def prepare_profile(root: Path) -> str:
    """Bootstrap, teach the competing import, grant trust. Returns repo_id."""
    bootstrap_repo(str(root))
    rid = _compute_repo_id(root)
    arch = (get_archetype(rid, str(root / "src/services/userService.ts")).get("data") or {}).get(
        "archetype"
    ) or "service"
    teach_competing_import(rid, archetype=arch, preferred=_NEW, over=_OLD)
    grant_trust(rid, root / ".chameleon")
    return rid


def _score(root: Path) -> str:
    f = root / "src/services/shippingService.ts"
    if not f.is_file():
        return "no-file"
    txt = f.read_text()
    new = bool(re.search(r'from\s+["\']\./httpClient["\']', txt))
    old = bool(re.search(r'from\s+["\']\./http["\']', txt))
    if new and not old:
        return "httpClient"
    if old and not new:
        return "http"
    return "both" if new and old else "neither"


# The static-CLAUDE.md arm: the same convention a human would hand-write, no
# plugin. Tests whether chameleon's derive+enforce beats "just put it in
# CLAUDE.md" — the north-star bar's comparison arm and the sharpest skeptic test.
_STATIC_CLAUDE_MD = (
    "# Project conventions\n\n"
    "- In `src/services`, always import the HTTP helper from `./httpClient` "
    "(functions `httpGet` / `httpPost`). Do NOT import from `./http`; that "
    "module is being retired.\n"
)


def _chameleon_conventions_md(root: Path) -> str:
    """Render the profile's conventions the way the SessionStart block does,
    stripped of the <chameleon-conventions> wrapper — the content a
    chameleon-maintained `.chameleon/conventions.md` would carry."""
    import json as _json

    from chameleon_mcp.conventions import format_conventions_for_session

    conv = _json.loads((root / ".chameleon" / "conventions.json").read_text())
    block = format_conventions_for_session(conv)
    lines = [
        ln
        for ln in block.splitlines()
        if ln.strip() not in ("<chameleon-conventions>", "</chameleon-conventions>")
    ]
    return "\n".join(lines).strip() + "\n"


def run_cell(src: Path, work: Path, arm: str, i: int, model: str) -> dict:
    dest = work / f"{arm}_{i}"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    (dest / "src/services/shippingService.ts").unlink(missing_ok=True)
    grant_trust(_compute_repo_id(dest), dest / ".chameleon")
    if arm == "off":
        env, plugin_root = {"CHAMELEON_DISABLE": "1"}, None
    elif arm == "static":
        # human-written rule, plugin disabled -> isolates chameleon vs CLAUDE.md
        (dest / "CLAUDE.md").write_text(_STATIC_CLAUDE_MD)
        env, plugin_root = {"CHAMELEON_DISABLE": "1"}, None
    elif arm in ("claudemd", "claudemd-noplugin"):
        # the product-vision arm: chameleon MATERIALIZES its derived conventions
        # into a file CLAUDE.md @-imports, marrying CLAUDE.md's authority with
        # chameleon's derivation/freshness. Plain variant isolates the channel.
        (dest / ".chameleon" / "conventions.md").write_text(_chameleon_conventions_md(dest))
        (dest / "CLAUDE.md").write_text("# Project notes\n\n@.chameleon/conventions.md\n")
        if arm == "claudemd":
            env, plugin_root = {"CHAMELEON_ALLOW_TMP_REPO": "1"}, _PLUGIN
        else:
            env, plugin_root = {"CHAMELEON_DISABLE": "1"}, None
    elif arm == "shadow":
        # chameleon active but advisory-only (no deny, so no escape-hatch prompt
        # for the model to rationalize around) -> isolates whether the DENY is
        # what backfires vs the plain counterexample injection.
        env, plugin_root = {"CHAMELEON_ALLOW_TMP_REPO": "1", "CHAMELEON_ENFORCE": "0"}, _PLUGIN
    else:  # "on" -> chameleon active, enforcing (deny)
        env, plugin_root = {"CHAMELEON_ALLOW_TMP_REPO": "1"}, _PLUGIN
    try:
        sess = spawn_claude(
            _PROMPT,
            dest,
            env,
            work / f"{arm}_{i}.jsonl",
            max_turns=15,
            permission_mode="bypassPermissions",
            model=model,
            plugin_root=plugin_root,
            disallowed_tools=["Agent", "Task", "Skill", "ScheduleWakeup", "Workflow"],
            timeout_s=300,
        )
        cost = getattr(sess, "cost_usd", 0.0)
    except Exception as e:
        return {"arm": arm, "i": i, "verdict": f"spawn-error:{e}", "cost": 0.0}
    return {"arm": arm, "i": i, "verdict": _score(dest), "cost": cost}


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    model = sys.argv[2] if len(sys.argv) > 2 else "sonnet"
    arm_arg = sys.argv[3] if len(sys.argv) > 3 else "both"
    if arm_arg in ("both", "all"):
        arms = ("off", "on")
    elif arm_arg in ("three", "all3"):
        arms = ("off", "static", "on")
    else:
        arms = tuple(arm_arg.split(","))
    tmp = Path(tempfile.mkdtemp(prefix="chameleon-mig-"))
    src, work = tmp / "src_repo", tmp / "work"
    work.mkdir()
    build_fixture(src)
    prepare_profile(src)
    print(f"fixture: 5 services on {_OLD}, 1 on {_NEW}; taught prefer {_NEW}. model={model}\n")
    rows = []
    for arm in arms:
        for i in range(n):
            r = run_cell(src, work, arm, i, model)
            rows.append(r)
            print(f"  {arm}#{i}: {r['verdict']}  (${r['cost']:.3f})", flush=True)

    def units(arm):
        return [(1 if r["verdict"] == "httpClient" else 0, 1) for r in rows if r["arm"] == arm]

    print("\n=== summary ===")
    for arm in arms:
        u = units(arm)
        c = sum(x for x, _ in u)
        print(f"  {arm}: {c}/{len(u)} chose {_NEW} (correct)")
    # each chameleon variant vs each guidance baseline
    for active in ("on", "shadow", "claudemd", "claudemd-noplugin"):
        for base in ("off", "static"):
            if active in arms and base in arms:
                r = two_sample_boot(units(active), units(base), resamples=20000)
                print(
                    f"  diff ({active} - {base}): {r['diff']:+.0f}pp  "
                    f"95% CI [{r['lo']:.0f}, {r['hi']:.0f}]  excludes zero: {r['lo'] > 0}"
                )
    print(f"  total cost: ${sum(r['cost'] for r in rows):.2f}")
    print(f"  (fixture + worktrees under {tmp}; delete when done)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
