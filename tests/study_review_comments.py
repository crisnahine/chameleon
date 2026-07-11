"""Real-world effectiveness study — H2: PR review-comment rate before/after adoption.

Pre-registered in docs/effectiveness-study.md (secondary time series). Walks
first-parent merge commits on the production ref, extracts each PR number and
its merge date, fetches the PR's non-deleted Bitbucket comment count (read-only,
via curl with the env creds), and buckets by pre/post the 2026-06-01 adoption
line. Reports mean comments/PR in each window.

Comment counts are cached under the scratch cache dir keyed by slug+PR so a
re-run is free and network-light. No agent spawns, no spend beyond read-only
Bitbucket GETs.

Usage:
    BITBUCKET_USER=... BITBUCKET_TOKEN=... \\
    CHAMELEON_TEST_TS_REPO=/abs/ef-client CHAMELEON_TEST_RUBY_REPO=/abs/ef-api \\
      PYTHONPATH=. plugin/mcp/.venv/bin/python tests/study_review_comments.py

A PR whose comments cannot be fetched is excluded (counted as fetch_failed).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

_ADOPTION = "2026-06-01"
_SINCE = "2026-01-01"
_DEFAULT_REF = "origin/production"
_API = "https://api.bitbucket.org/2.0/repositories"
_PR_RE = re.compile(r"pull request #(\d+)")
_CACHE_DIR = Path(
    os.environ.get("CHAMELEON_STUDY_CACHE")
    or (Path(__file__).resolve().parent / "effectiveness" / ".study_cache")
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True).stdout


def _slug(repo: Path) -> str | None:
    url = _git(repo, "remote", "get-url", "origin").strip()
    m = re.search(r"[:/](empire-flippers/[A-Za-z0-9_.-]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def _creds() -> tuple[str, str] | None:
    u, t = os.environ.get("BITBUCKET_USER"), os.environ.get("BITBUCKET_TOKEN")
    return (u, t) if u and t else None


def _resolve_ref(repo: Path) -> str:
    ref = os.environ.get("CHAMELEON_STUDY_REF", _DEFAULT_REF)
    if (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref], capture_output=True
        ).returncode
        != 0
    ):
        return "HEAD"
    return ref


def _comment_count(slug: str, pr: str) -> int | None:
    cache = _CACHE_DIR / slug.replace("/", "_")
    cache.mkdir(parents=True, exist_ok=True)
    cf = cache / f"pr-{pr}.json"
    if cf.is_file():
        try:
            return json.loads(cf.read_text()).get("nd")
        except Exception:
            pass
    creds = _creds()
    if creds is None:
        return None
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
        return None
    vals = d.get("values")
    if vals is None:
        return None
    nd = sum(1 for c in vals if not c.get("deleted"))
    try:
        cf.write_text(json.dumps({"nd": nd, "size": d.get("size")}))
    except Exception:
        pass
    return nd


def measure(repo_path: str) -> dict:
    repo = Path(repo_path)
    slug = _slug(repo)
    if not slug:
        return {"repo": str(repo), "error": "no empire-flippers origin slug"}
    ref = _resolve_ref(repo)
    log = _git(repo, "log", "--first-parent", "--format=%s|%cs", "--since", _SINCE, ref)
    pre = {"prs": 0, "comments": 0}
    post = {"prs": 0, "comments": 0}
    fetch_failed = 0
    per_month: dict[str, dict] = {}
    # per-PR rows are the bootstrap unit for the comment-rate difference.
    prs: list[dict] = []
    for line in log.splitlines():
        subj, _, date = line.rpartition("|")
        mo = _PR_RE.search(subj)
        if not mo or not date:
            continue
        nd = _comment_count(slug, mo.group(1))
        if nd is None:
            fetch_failed += 1
            continue
        month = date[:7]
        pm = per_month.setdefault(month, {"prs": 0, "comments": 0})
        pm["prs"] += 1
        pm["comments"] += nd
        arm = "post" if date >= _ADOPTION else "pre"
        bucket = post if arm == "post" else pre
        bucket["prs"] += 1
        bucket["comments"] += nd
        prs.append({"pr": mo.group(1), "date": date, "arm": arm, "comments": nd})

    def mean(b):
        return round(b["comments"] / b["prs"], 2) if b["prs"] else None

    months = [
        {
            "month": m,
            "prs": per_month[m]["prs"],
            "comments": per_month[m]["comments"],
            "mean_comments_per_pr": mean(per_month[m]),
        }
        for m in sorted(per_month)
    ]
    return {
        "repo": str(repo),
        "slug": slug,
        "ref": ref,
        "adoption_month": _ADOPTION[:7],
        "fetch_failed": fetch_failed,
        "pre_prs": pre["prs"],
        "post_prs": post["prs"],
        "pre_mean_comments_per_pr": mean(pre),
        "post_mean_comments_per_pr": mean(post),
        "prs": prs,
        "months": months,
    }


def main() -> int:
    targets = [
        ("ef-client (TS)", os.environ.get("CHAMELEON_TEST_TS_REPO")),
        ("ef-api (Rails)", os.environ.get("CHAMELEON_TEST_RUBY_REPO")),
    ]
    results = []
    for label, path in targets:
        if not path or not Path(path).is_dir():
            print(f"SKIP {label}: no repo", file=sys.stderr)
            continue
        print(f"=== H2 review-comment rate: {label} ===", file=sys.stderr)
        r = measure(path)
        r["label"] = label
        results.append(r)
        if r.get("error"):
            print(f"  ERROR: {r['error']}", file=sys.stderr)
            continue
        print(
            f"  pre-adoption:  {r['pre_mean_comments_per_pr']} comments/PR (n={r['pre_prs']} PRs)",
            file=sys.stderr,
        )
        print(
            f"  post-adoption: {r['post_mean_comments_per_pr']} comments/PR (n={r['post_prs']} PRs)"
            f"  [{r['fetch_failed']} fetch-failed]",
            file=sys.stderr,
        )
        for m in r["months"]:
            mark = " <- adoption" if m["month"] == r["adoption_month"] else ""
            print(
                f"    {m['month']}: {m['mean_comments_per_pr']} ({m['prs']} PRs){mark}",
                file=sys.stderr,
            )
    print(json.dumps({"study": "H2_review_comment_rate", "results": results}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
