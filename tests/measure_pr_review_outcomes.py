"""Outcome-side measurement: do the PRs chameleon routes review-clean actually
draw zero review comments?

Correlates the auto-pass router's per-PR verdict (complexity tier +
auto_pass_eligible) with the REAL review-comment count each PR received on
Bitbucket. No agent spawns and no spend: deterministic router over git history +
read-only Bitbucket comment counts (bbcurl). This is the outcome the goal names
("zero-review-comment PRs"), as opposed to the routing-side decision alone.

Usage:
    CHAMELEON_TEST_TS_REPO=/abs/ts-repo CHAMELEON_TEST_RUBY_REPO=/abs/rails-repo \\
      PYTHONPATH=. mcp/.venv/bin/python tests/measure_pr_review_outcomes.py [N]

Requires bbcurl (curl -u $BITBUCKET_USER:$BITBUCKET_TOKEN) on PATH. Comment count
is non-deleted PR comments (inline + general); a PR with >100 comments is flagged
truncated. A PR whose comments cannot be fetched is excluded from the outcome
stats (counted as fetch_failed).
"""

from __future__ import annotations

import json
import os
import re
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
_PR_RE = re.compile(r"pull request #(\d+)")
_API = "https://api.bitbucket.org/2.0/repositories"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True).stdout


def _slug(repo: Path) -> str | None:
    url = _git(repo, "remote", "get-url", "origin").strip()
    m = re.search(r"[:/](empire-flippers/[A-Za-z0-9_.-]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def _bb_creds() -> tuple[str, str] | None:
    user = os.environ.get("BITBUCKET_USER")
    token = os.environ.get("BITBUCKET_TOKEN")
    return (user, token) if user and token else None


def _comment_count(slug: str, pr: str) -> tuple[int | None, bool]:
    """(non-deleted comment count, truncated). (None, False) on fetch failure.

    Calls curl directly (bbcurl is a shell function, not an executable a
    subprocess can exec) with the Bitbucket creds from the environment.
    """
    creds = _bb_creds()
    if creds is None:
        return None, False
    url = f"{_API}/{slug}/pullrequests/{pr}/comments?pagelen=100"
    try:
        out = subprocess.run(
            ["curl", "-s", "-u", f"{creds[0]}:{creds[1]}", url],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
        d = json.loads(out)
    except Exception:
        return None, False
    vals = d.get("values")
    if vals is None:
        return None, False
    nd = sum(1 for c in vals if not c.get("deleted"))
    size = d.get("size")
    truncated = isinstance(size, int) and size > 100
    return nd, truncated


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
    slug = _slug(repo_root)
    if not slug:
        return {"repo": repo, "error": "no empire-flippers origin slug", "rows": []}
    is_un, imp, blk = _adapters(repo_root, repo_id)
    lines = _git(repo_root, "log", "--first-parent", "--format=%H %s", "-n", str(n)).splitlines()
    rows: list[dict] = []
    for line in lines:
        h, _, subj = line.partition(" ")
        mo = _PR_RE.search(subj)
        if not mo:
            continue
        pr = mo.group(1)
        base = _git(repo_root, "rev-parse", f"{h}^1").strip()
        if not base or base == h:
            continue
        numstat = _git(repo_root, "diff", "--numstat", base, h)
        if not numstat.strip():
            continue
        name_status = _git(repo_root, "diff", "--name-status", base, h)
        diff = _git(repo_root, "diff", base, h)[:_DIFF_CAP]
        v = build_autopass_verdict(
            numstat,
            name_status,
            is_unarchetyped=is_un,
            importers_of=imp,
            block_findings_for=blk,
            diff_text=diff,
        )
        nd, trunc = _comment_count(slug, pr)
        rows.append(
            {
                "pr": pr,
                "tier": v.get("complexity_tier", "complex"),
                "eligible": bool(v.get("auto_pass_eligible")),
                "comments": nd,
                "truncated": trunc,
                "facts": v.get("facts") or {},
            }
        )
    return {"repo": repo, "slug": slug, "rows": rows}


def _dump_false_passes(res: dict) -> None:
    """Print the FALSE-PASSES (routed review-clean but actually drew comments) with
    the facts that let them through, to diagnose the missed signal."""
    rows = [r for r in res.get("rows", []) if r["comments"] is not None]
    fp = [r for r in rows if r["eligible"] and r["comments"] > 0]
    if not fp:
        return
    print(f"\n  FALSE-PASSES in {res.get('slug')} ({len(fp)}):")
    for r in sorted(fp, key=lambda x: -x["comments"]):
        f = r["facts"]
        print(
            f"   PR#{r['pr']:<5} {r['tier']:<7} comments={r['comments']:<3} "
            f"files={f.get('files_changed')} lines={f.get('lines_changed')} "
            f"new={f.get('new_files')} unarch={f.get('unarchetyped_files')} "
            f"blast={f.get('blast_radius')}/{f.get('blast_radius_unknown')}? "
            f"sec={f.get('security_surface')}"
        )


def _pct(num: int, den: int) -> str:
    return f"{(num / den * 100):.0f}%" if den else "-"


def _identification_claim(fetched: list[dict]) -> dict:
    """The de-confounded identification claim: PRECISION (of PRs routed
    review-clean, fraction that drew 0 real comments, Wilson 95% lower-bounded)
    and RECALL (fraction of easy+medium PRs routed review-clean)."""
    from tests.effectiveness.stats import wilson_lower_bound

    elig = [r for r in fetched if r["eligible"]]
    elig_zero = sum(1 for r in elig if r["comments"] == 0)
    routine = [r for r in fetched if r["tier"] in ("easy", "medium")]
    routine_clean = sum(1 for r in routine if r["eligible"])
    return {
        "eligible_n": len(elig),
        "eligible_zero": elig_zero,
        "precision": (elig_zero / len(elig)) if elig else None,
        "precision_wilson_lo": wilson_lower_bound(elig_zero, len(elig)),
        "routine_n": len(routine),
        "routine_clean": routine_clean,
        "recall": (routine_clean / len(routine)) if routine else None,
    }


def _report(res: dict) -> None:
    print(f"\n=== {res.get('repo')} ({res.get('slug')}) ===")
    if res.get("error"):
        print("  error:", res["error"])
        return
    rows = res["rows"]
    fetched = [r for r in rows if r["comments"] is not None]
    failed = len(rows) - len(fetched)
    print(f"PRs: {len(rows)} | comment-fetched: {len(fetched)} | fetch-failed: {failed}")

    # The headline: of the PRs chameleon routed review-clean, how many actually
    # drew zero review comments?
    elig = [r for r in fetched if r["eligible"]]
    inelig = [r for r in fetched if not r["eligible"]]
    elig_zero = sum(1 for r in elig if r["comments"] == 0)
    inelig_zero = sum(1 for r in inelig if r["comments"] == 0)
    c = _identification_claim(fetched)
    print(
        f"review-clean routed:  {len(elig):>3} PRs | 0-comment: {elig_zero} "
        f"({_pct(elig_zero, len(elig))}) | precision Wilson95 LB: {c['precision_wilson_lo']:.2f}"
    )
    print(
        f"human-routed:         {len(inelig):>3} PRs | "
        f"actually 0 comments: {inelig_zero} ({_pct(inelig_zero, len(inelig))})"
    )
    print(
        f"recall of easy+medium routed clean: {c['routine_clean']}/{c['routine_n']} "
        f"({_pct(c['routine_clean'], c['routine_n'])})"
    )

    print(f"\n{'tier':<9} {'PRs':>4} {'routed-clean':>13} {'0-comment':>10} {'mean cmts':>10}")
    for tier in _TIERS:
        trows = [r for r in fetched if r["tier"] == tier]
        if not trows:
            print(f"{tier:<9} {0:>4}")
            continue
        clean = sum(1 for r in trows if r["eligible"])
        zero = sum(1 for r in trows if r["comments"] == 0)
        mean = sum(r["comments"] for r in trows) / len(trows)
        print(
            f"{tier:<9} {len(trows):>4} {_pct(clean, len(trows)):>13} "
            f"{_pct(zero, len(trows)):>10} {mean:>10.1f}"
        )


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    repos = [
        r
        for r in (
            os.environ.get("CHAMELEON_TEST_TS_REPO"),
            os.environ.get("CHAMELEON_TEST_RUBY_REPO"),
        )
        if r
    ]
    if not repos:
        print("set CHAMELEON_TEST_TS_REPO and/or CHAMELEON_TEST_RUBY_REPO")
        return 2
    pooled: list[dict] = []
    for repo in repos:
        res = measure(repo, n)
        _report(res)
        _dump_false_passes(res)
        pooled.extend(r for r in res.get("rows", []) if r["comments"] is not None)

    if pooled:
        c = _identification_claim(pooled)
        print("\n=== POOLED IDENTIFICATION CLAIM (both repos) ===")
        print(f"comment-fetched PRs: {len(pooled)}")
        if c["precision"] is not None:
            print(
                f"PRECISION: {c['eligible_zero']}/{c['eligible_n']} review-clean-routed PRs "
                f"drew 0 comments = {c['precision'] * 100:.0f}% "
                f"(Wilson 95% lower bound {c['precision_wilson_lo']:.2f})"
            )
        if c["recall"] is not None:
            print(
                f"RECALL: {c['routine_clean']}/{c['routine_n']} easy+medium PRs routed "
                f"review-clean = {c['recall'] * 100:.0f}%"
            )
        print(
            "CAVEATS: denominator = comment-fetched PRs only (fetch-failed and >100-comment "
            "excluded); comment count = non-deleted Bitbucket PR comments (undercounts "
            "approve/request-changes states + Slack review); LOOK-AHEAD: the profile is derived "
            "from a ref AFTER these PRs merged, so precision is an upper bound until a temporal "
            "holdout (profile pinned before the window) is run."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
