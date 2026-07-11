"""Real-world effectiveness study — D1 interrupted time series (deterministic, free).

Pre-registered in docs/effectiveness-study.md. For each calendar month of a
dogfood repo's mainline history, replays chameleon's own lint_file over the
source files each first-parent commit changed, and reports the month's
new-violation rate (violations flagged in the current file / files scored)
against the 2026-06-01 chameleon-adoption line. No LLM spawns, no spend.

The lint is run against the file's CURRENT content at each historical commit
(git-show of the blob), scored under the repo's committed profile — the same
deterministic engine tests/effectiveness/scorers/convention.py uses. This is a
STRUCTURAL-conformance signal (the dimension lint_file measures); the study doc
records that idiom-level conformance is not lint-scored without taught rules.

Usage:
    CHAMELEON_TEST_TS_REPO=/abs/ef-client CHAMELEON_TEST_RUBY_REPO=/abs/ef-api \\
      PYTHONPATH=. plugin/mcp/.venv/bin/python tests/study_retrospective.py [months]

Requires a committed .chameleon profile in each repo. Fails open per file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from chameleon_mcp.tools import _compute_repo_id, get_archetype, lint_file

_ADOPTION = "2026-06-01"
_SINCE = "2026-01-01"  # study window start; pre-adoption arm is _SINCE.._ADOPTION
# origin's mainline, not local HEAD: the QA clones carry diverged local
# experiment branches, so the real production history lives on the remote ref.
_DEFAULT_REF = "origin/production"
_SRC_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".rb", ".py")
_MAX_BYTES = 400_000


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True).stdout


def _changed_src_at(repo: Path, sha: str) -> list[str]:
    out = _git(
        repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "-m", "--first-parent", sha
    )
    return [f for f in out.splitlines() if f.lower().endswith(_SRC_EXTS)]


def _blob_at(repo: Path, sha: str, rel: str) -> str | None:
    r = subprocess.run(["git", "-C", str(repo), "show", f"{sha}:{rel}"], capture_output=True)
    if r.returncode != 0 or len(r.stdout) > _MAX_BYTES:
        return None
    return r.stdout.decode("utf-8", errors="replace")


def _resolve_ref(repo: Path) -> str:
    ref = os.environ.get("CHAMELEON_STUDY_REF", _DEFAULT_REF)
    # fall back to HEAD if the preferred remote ref is absent in this clone
    if (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
            capture_output=True,
        ).returncode
        != 0
    ):
        return "HEAD"
    return ref


def measure(repo_path: str, n_commits: int) -> dict:
    repo = Path(repo_path)
    rid = _compute_repo_id(repo)
    ref = _resolve_ref(repo)
    # first-parent mainline commits on the production ref, since the window
    # start, newest first, with commit date
    log = _git(repo, "log", "--first-parent", "--format=%H|%cs", "--since", _SINCE, ref)
    per_month: dict[str, dict] = defaultdict(lambda: {"files": 0, "violations": 0, "commits": 0})
    # per-commit rows are the bootstrap unit: each carries (files, violations)
    # under its pre/post-adoption arm, so stats can resample over commits.
    commits: list[dict] = []
    for line in log.splitlines():
        if "|" not in line:
            continue
        sha, date = line.split("|", 1)
        month = date[:7]
        srcs = _changed_src_at(repo, sha)
        if not srcs:
            continue
        per_month[month]["commits"] += 1
        c_files = 0
        c_viol = 0
        for rel in srcs[:50]:  # bound huge commits
            content = _blob_at(repo, sha, rel)
            if content is None:
                continue
            try:
                arch = (get_archetype(rid, str(repo / rel)).get("data") or {}).get(
                    "archetype"
                ) or "none"
                out = lint_file(rid, arch, content, str(repo / rel)).get("data") or {}
                c_files += 1
                c_viol += len(out.get("violations") or [])
            except Exception:
                continue
        per_month[month]["files"] += c_files
        per_month[month]["violations"] += c_viol
        if c_files:
            commits.append(
                {
                    "sha": sha[:12],
                    "date": date,
                    "arm": "post" if date >= _ADOPTION else "pre",
                    "files": c_files,
                    "violations": c_viol,
                }
            )
    # assemble rate per month + pre/post adoption aggregates
    rows = []
    pre = {"files": 0, "violations": 0}
    post = {"files": 0, "violations": 0}
    for month in sorted(per_month):
        m = per_month[month]
        rate = round(100 * m["violations"] / m["files"], 2) if m["files"] else None
        rows.append({"month": month, **m, "viol_per_100_files": rate})
        bucket = post if month >= _ADOPTION[:7] else pre
        bucket["files"] += m["files"]
        bucket["violations"] += m["violations"]

    def agg(b):
        return round(100 * b["violations"] / b["files"], 2) if b["files"] else None

    return {
        "repo": str(repo),
        "ref": ref,
        "adoption_month": _ADOPTION[:7],
        "commits": commits,
        "months": rows,
        "pre_adoption_viol_per_100": agg(pre),
        "post_adoption_viol_per_100": agg(post),
        "pre_files": pre["files"],
        "post_files": post["files"],
    }


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    targets = [
        ("ef-client (TS)", os.environ.get("CHAMELEON_TEST_TS_REPO")),
        ("ef-api (Rails)", os.environ.get("CHAMELEON_TEST_RUBY_REPO")),
    ]
    results = []
    for label, path in targets:
        if not path or not (Path(path) / ".chameleon" / "profile.json").is_file():
            print(f"SKIP {label}: no repo / no committed profile", file=sys.stderr)
            continue
        print(f"=== D1 time series: {label} ===", file=sys.stderr)
        r = measure(path, n)
        r["label"] = label
        results.append(r)
        # human summary -> stderr; stdout stays pure JSON for study_analyze.py
        print(
            f"  pre-adoption:  {r['pre_adoption_viol_per_100']} viol/100 files (n={r['pre_files']})",
            file=sys.stderr,
        )
        print(
            f"  post-adoption: {r['post_adoption_viol_per_100']} viol/100 files (n={r['post_files']})",
            file=sys.stderr,
        )
        for m in r["months"]:
            mark = " <- adoption" if m["month"] == r["adoption_month"] else ""
            print(
                f"    {m['month']}: {m['viol_per_100_files']} "
                f"({m['commits']} commits, {m['files']} files){mark}",
                file=sys.stderr,
            )
    print(json.dumps({"study": "D1_interrupted_time_series", "results": results}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
