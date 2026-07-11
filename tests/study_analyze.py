"""Real-world effectiveness study — analysis: bootstrap CIs on the pre/post gap.

Consumes the D1 (per-commit) and H2 (per-PR) JSON emitted by
study_retrospective.py and study_review_comments.py, and computes a two-sample
cluster bootstrap 95% CI on the difference in the pooled rate between the
pre-adoption and post-adoption arms.

The unit of resampling is the COMMIT (D1) or the PR (H2): the pooled rate within
an arm is sum(numer)/sum(denom) over the resampled units, so a commit that
changed more files carries proportional weight without inflating n. The reported
quantity is (pre_rate - post_rate); a POSITIVE lower bound means the post-adoption
arm has a genuinely lower rate (an improvement). Per docs/effectiveness-study.md,
a hypothesis is "supported" only when the CI excludes zero in the predicted
(improvement) direction.

Usage:
    PYTHONPATH=. plugin/mcp/.venv/bin/python tests/study_analyze.py \\
        <d1.json> <h2.json>
"""

from __future__ import annotations

import json
import random
import sys


def _pooled_rate(units: list[tuple[float, float]]) -> float | None:
    """units: list of (numer, denom). Pooled rate = sum(numer)/sum(denom)*100."""
    dn = sum(d for _, d in units)
    return (100 * sum(n for n, _ in units) / dn) if dn else None


def two_sample_boot(
    pre: list[tuple[float, float]],
    post: list[tuple[float, float]],
    *,
    resamples: int = 10000,
    ci: float = 0.95,
    seed: int = 12345,
) -> dict:
    """CI on (pre_pooled_rate - post_pooled_rate), resampling units w/ replacement."""
    pr = _pooled_rate(pre)
    po = _pooled_rate(post)
    if pr is None or po is None:
        return {
            "pre_rate": pr,
            "post_rate": po,
            "diff": None,
            "lo": None,
            "hi": None,
            "n_pre": len(pre),
            "n_post": len(post),
        }
    rng = random.Random(seed)
    diffs: list[float] = []
    npre, npost = len(pre), len(post)
    for _ in range(resamples):
        rs_pre = [pre[rng.randrange(npre)] for _ in range(npre)]
        rs_post = [post[rng.randrange(npost)] for _ in range(npost)]
        a, b = _pooled_rate(rs_pre), _pooled_rate(rs_post)
        if a is not None and b is not None:
            diffs.append(a - b)
    diffs.sort()
    lo_i = int((1 - ci) / 2 * len(diffs))
    hi_i = min(len(diffs) - 1, int((1 + ci) / 2 * len(diffs)))
    return {
        "pre_rate": round(pr, 2),
        "post_rate": round(po, 2),
        "diff": round(pr - po, 2),
        "lo": round(diffs[lo_i], 2),
        "hi": round(diffs[hi_i], 2),
        "n_pre": npre,
        "n_post": npost,
        "supported": diffs[lo_i] > 0,  # improvement CI excludes zero
    }


def _verdict(res: dict) -> str:
    if res["diff"] is None:
        return "NO DATA"
    if res["lo"] > 0:
        return "SUPPORTED (post lower, CI excludes 0)"
    if res["hi"] < 0:
        return "REVERSED (post higher, CI excludes 0)"
    return "NULL (CI straddles 0)"


def analyze_d1(path: str) -> list[dict]:
    data = json.loads(open(path).read())
    out = []
    for r in data.get("results", []):
        commits = r.get("commits") or []
        pre = [(c["violations"], c["files"]) for c in commits if c["arm"] == "pre"]
        post = [(c["violations"], c["files"]) for c in commits if c["arm"] == "post"]
        res = two_sample_boot(pre, post)
        res["label"] = r.get("label")
        res["metric"] = "viol_per_100_files"
        res["verdict"] = _verdict(res)
        out.append(res)
    return out


def analyze_h2(path: str) -> list[dict]:
    data = json.loads(open(path).read())
    out = []
    for r in data.get("results", []):
        if r.get("error"):
            continue
        prs = r.get("prs") or []
        # unit = PR; numer = comment count, denom = 1 (mean comments/PR)
        pre = [(p["comments"], 1.0) for p in prs if p["arm"] == "pre"]
        post = [(p["comments"], 1.0) for p in prs if p["arm"] == "post"]
        res = two_sample_boot(pre, post)
        res["label"] = r.get("label")
        res["metric"] = "comments_per_pr"
        # rates here are *100 of a per-PR mean; undo the *100 for readability
        for k in ("pre_rate", "post_rate", "diff", "lo", "hi"):
            if res.get(k) is not None:
                res[k] = round(res[k] / 100, 3)
        res["verdict"] = _verdict(res)
        out.append(res)
    return out


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: study_analyze.py <d1.json> <h2.json>", file=sys.stderr)
        return 2
    d1 = analyze_d1(sys.argv[1])
    h2 = analyze_h2(sys.argv[2])
    print("=== D1 new-violation rate (viol/100 changed files), pre - post ===")
    for r in d1:
        print(
            f"  {r['label']:<18} pre={r['pre_rate']} post={r['post_rate']} "
            f"diff={r['diff']} 95%CI[{r['lo']}, {r['hi']}] "
            f"(n_pre={r['n_pre']} n_post={r['n_post']}) -> {r['verdict']}"
        )
    print("\n=== H2 review comments per PR, pre - post ===")
    for r in h2:
        print(
            f"  {r['label']:<18} pre={r['pre_rate']} post={r['post_rate']} "
            f"diff={r['diff']} 95%CI[{r['lo']}, {r['hi']}] "
            f"(n_pre={r['n_pre']} n_post={r['n_post']}) -> {r['verdict']}"
        )
    print()
    print(json.dumps({"D1": d1, "H2": h2}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
