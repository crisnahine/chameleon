"""Free measurement: replay real merged PRs through the auto-pass router and
tabulate the per-complexity-tier review-clean (auto-pass-eligible) rate.

No `claude -p` spawns and no profile mutation: pure deterministic router over
real git history, using the repo's committed profile for the archetype / cross-
file / block-finding adapters. Answers the goal's question directly -- across
real PRs, what share of each tier (easy / medium / hard / complex) does chameleon
route as a review-clean candidate, and where does the human residual sit?

Usage:
    CHAMELEON_TEST_TS_REPO=/abs/ts-repo CHAMELEON_TEST_RUBY_REPO=/abs/rails-repo \\
      PYTHONPATH=. mcp/.venv/bin/python tests/measure_pr_tier_distribution.py [N]

Caveat: the archetype / importer / lint adapters read the CURRENT profile and
file state, so a PR that reshaped a file since merge is scored against today's
conventions; the structural facts (files / lines / new files / security surface
/ test integrity) are exact per-PR from the historical diff.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from chameleon_mcp.autopass import build_autopass_verdict
from chameleon_mcp.enforcement_calibration import active_block_rules
from chameleon_mcp.safe_open import safe_read_text
from chameleon_mcp.tools import (
    _compute_repo_id,
    get_archetype,
    lint_file,
    query_symbol_importers,
)

_DIFF_CAP = 200_000
_TS_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_TIERS = ("easy", "medium", "hard", "complex")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True).stdout


def _adapters(repo_root: Path, repo_id: str):
    try:
        active = active_block_rules(repo_root / ".chameleon")
    except Exception:
        active = set()

    def arch_of(rel: str):
        try:
            d = get_archetype(repo_id, str(repo_root / rel)).get("data") or {}
            return d.get("archetype"), d.get("match_quality")
        except Exception:
            return None, "none"

    def is_unarchetyped(rel: str) -> bool:
        a, mq = arch_of(rel)
        return not a or mq in ("none", "fallback")

    def importers_of(rel: str):
        if not str(rel).lower().endswith(_TS_EXTS):
            return 0
        try:
            d = query_symbol_importers(repo_id, str(repo_root / rel)).get("data") or {}
            if not d.get("found"):
                return None
            return sum(int(i.get("count", 0)) for i in (d.get("importers") or []))
        except Exception:
            return None

    def block_findings_for(rel: str) -> int:
        if not active:
            return 0
        a, _ = arch_of(rel)
        if not a:
            return 0
        try:
            content = safe_read_text(repo_root, rel)
            d = lint_file(repo_id, a, content, str(repo_root / rel)).get("data") or {}
            return sum(1 for v in (d.get("violations") or []) if v.get("rule") in active)
        except Exception:
            return 0

    return is_unarchetyped, importers_of, block_findings_for


def measure(repo: str, n: int) -> dict:
    repo_root = Path(repo)
    repo_id = _compute_repo_id(repo_root)
    is_un, imp, blk = _adapters(repo_root, repo_id)
    # First-parent mainline commits stand in for merged PRs across both workflows:
    # a merge commit (Bitbucket "Merged in ...") or a squashed commit each carries
    # one PR's net change as ``<commit>^1..<commit>``.
    merges = _git(repo_root, "log", "--first-parent", "--format=%H", "-n", str(n)).split()
    per_tier: dict = {t: {"n": 0, "eligible": 0} for t in _TIERS}
    scored = 0
    for m in merges:
        base = _git(repo_root, "rev-parse", f"{m}^1").strip()
        if not base or base == m:
            continue
        numstat = _git(repo_root, "diff", "--numstat", base, m)
        if not numstat.strip():
            continue
        name_status = _git(repo_root, "diff", "--name-status", base, m)
        diff = _git(repo_root, "diff", base, m)[:_DIFF_CAP]
        v = build_autopass_verdict(
            numstat,
            name_status,
            is_unarchetyped=is_un,
            importers_of=imp,
            block_findings_for=blk,
            diff_text=diff,
        )
        tier = v.get("complexity_tier", "complex")
        per_tier.setdefault(tier, {"n": 0, "eligible": 0})
        per_tier[tier]["n"] += 1
        if v.get("auto_pass_eligible"):
            per_tier[tier]["eligible"] += 1
        scored += 1
    return {"repo": repo, "repo_id": repo_id, "scored": scored, "per_tier": per_tier}


def _report(res: dict) -> None:
    print(f"\n=== {res['repo']}  ({res['scored']} PRs scored) ===")
    print(f"{'tier':<9} {'PRs':>5} {'review-clean':>13} {'rate':>7}")
    total_n = total_e = 0
    for tier in _TIERS:
        row = res["per_tier"].get(tier, {"n": 0, "eligible": 0})
        n, e = row["n"], row["eligible"]
        total_n += n
        total_e += e
        rate = f"{(e / n * 100):.0f}%" if n else "-"
        print(f"{tier:<9} {n:>5} {e:>13} {rate:>7}")
    rate = f"{(total_e / total_n * 100):.0f}%" if total_n else "-"
    print(f"{'TOTAL':<9} {total_n:>5} {total_e:>13} {rate:>7}")


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    repos = [
        os.environ.get("CHAMELEON_TEST_TS_REPO"),
        os.environ.get("CHAMELEON_TEST_RUBY_REPO"),
    ]
    repos = [r for r in repos if r]
    if not repos:
        print("set CHAMELEON_TEST_TS_REPO and/or CHAMELEON_TEST_RUBY_REPO")
        return 2
    for repo in repos:
        _report(measure(repo, n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
