"""Framework-agnostic historical co-change mining from git history.

The curated cochange table (cochange.py) knows a handful of framework pairs
(Rails model -> migration, etc.). This mines the repo's OWN history: files that
change together across commits. If editing A has historically meant editing B
(B present in >= ``min_ratio`` of A's commits, with >= ``min_support`` commits),
a change that touches A but not B is a deterministic, zero-LLM omission signal --
"you usually change B when you change A".

Built once per bootstrap/refresh from a single bounded ``git log`` walk and
persisted to the plugin data dir, OFF the trust-hashed profile surface (like the
cross-workspace reverse index): it is advisory-only derived data, never a
security anchor. Reads git METADATA only -- no repo code runs, no network -- and
every seam fails open.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

from chameleon_mcp._thresholds import threshold_float, threshold_int

COCHANGE_HISTORY_FILENAME = "cochange_history.json"
_SCHEMA = 2  # v2: top-relative keys + a ``root`` field (v1 was workspace-relative)
# ``%x1f`` tells git to emit the ASCII Unit Separator (0x1f) once per commit; the
# byte marks a commit-header line in the combined ``git log --name-only`` stream
# (a tracked path cannot begin with it). A bare literal-byte format is rejected by
# git, so the FORMAT is the ``%x1f`` placeholder and the PARSE matches the byte.
_COMMIT_FORMAT = "%x1f"
_COMMIT_MARK = "\x1f"


def _git_toplevel(root: Path) -> Path | None:
    """The git work-tree top for ``root``, or None. Keys are top-relative so a
    monorepo's workspaces share ONE global index under the shared repo_id instead
    of overwriting each other with colliding workspace-relative paths."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=threshold_int("COCHANGE_HISTORY_GIT_TIMEOUT_SECONDS"),
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    top = (proc.stdout or "").strip()
    return Path(top) if proc.returncode == 0 and top else None


def _commit_file_groups(top: Path, max_commits: int) -> list[list[str]] | None:
    """Per-commit file lists from one bounded ``git log --name-only`` walk over the
    whole repo, or None when git is unavailable. Run at the work-tree top with NO
    ``--relative``, so every path is top-relative (globally unique across a
    monorepo's workspaces). Mirrors canonical._build_commit_time_map's subprocess
    discipline (single call, timeout, fail-open).
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(top),
                "-c",
                "core.quotePath=false",
                "log",
                "--no-renames",
                f"--format={_COMMIT_FORMAT}",
                "--name-only",
                "-n",
                str(max(1, max_commits)),
            ],
            capture_output=True,
            text=True,
            timeout=threshold_int("COCHANGE_HISTORY_GIT_TIMEOUT_SECONDS"),
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None

    groups: list[list[str]] = []
    current: list[str] | None = None
    for raw in (proc.stdout or "").split("\n"):
        if raw.startswith(_COMMIT_MARK):
            if current is not None:
                groups.append(current)
            current = []
            continue
        if current is None or not raw:
            continue
        current.append(raw)
    if current is not None:
        groups.append(current)
    return groups


def mine_cochange_history(
    repo_root,
    *,
    max_commits: int | None = None,
    max_files_per_commit: int | None = None,
    min_support: int | None = None,
    min_ratio: float | None = None,
    max_partners_per_file: int | None = None,
    max_files: int | None = None,
) -> dict | None:
    """Mine ``{file: [{partner, co, of, ratio}, ...]}`` from commit history, or
    None when git is unavailable. A partner B of A is a file that co-changed with
    A in >= ``min_support`` commits AND in >= ``min_ratio`` of A's commits; a
    partner that no longer exists in the tree is pruned (chasing a deleted file is
    an un-actionable false omission). A bulk commit touching more than
    ``max_files_per_commit`` files is skipped (a mass reformat / rename sweep
    would co-occur everything). Bounded at every axis; thresholds default from
    ``_thresholds`` but are overridable for testing.
    """
    root = Path(repo_root)
    if max_commits is None:
        max_commits = threshold_int("COCHANGE_HISTORY_MAX_COMMITS")
    if max_files_per_commit is None:
        max_files_per_commit = threshold_int("COCHANGE_HISTORY_MAX_FILES_PER_COMMIT")
    if min_support is None:
        min_support = threshold_int("COCHANGE_HISTORY_MIN_SUPPORT")
    if min_ratio is None:
        min_ratio = threshold_float("COCHANGE_HISTORY_MIN_RATIO")
    if max_partners_per_file is None:
        max_partners_per_file = threshold_int("COCHANGE_HISTORY_MAX_PARTNERS_PER_FILE")
    if max_files is None:
        max_files = threshold_int("COCHANGE_HISTORY_MAX_FILES")

    top = _git_toplevel(root)
    if top is None:
        return None
    groups = _commit_file_groups(top, max_commits)
    if groups is None:
        return None

    file_commits: Counter = Counter()
    co: dict = defaultdict(Counter)  # a -> Counter(partner -> co-occurrence count)
    for files in groups:
        if not files or len(files) > max_files_per_commit:
            continue
        uniq = sorted(set(files))
        for f in uniq:
            file_commits[f] += 1
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                a, b = uniq[i], uniq[j]
                co[a][b] += 1
                co[b][a] += 1

    partners: dict = {}
    for a, total in file_commits.items():
        if total < min_support:
            continue
        rows = []
        for b, c in co.get(a, {}).items():
            if c < min_support or (c / total) < min_ratio:
                continue
            if not (top / b).is_file():  # pruned: partner no longer in the tree
                continue
            rows.append({"partner": b, "co": c, "of": total, "ratio": round(c / total, 3)})
        if rows:
            rows.sort(key=lambda r: (-r["ratio"], -r["co"], r["partner"]))
            partners[a] = rows[:max_partners_per_file]

    if len(partners) > max_files:  # bound the artifact: keep the strongest pairings
        ranked = sorted(partners.items(), key=lambda kv: -kv[1][0]["ratio"])
        partners = dict(ranked[:max_files])

    # ``root`` is the work-tree top the keys are relative to; the consumer uses it
    # to relativize the turn's edited files (same machine/checkout, rebuilt on
    # refresh) rather than spawning git on the Stop hot path.
    return {"schema": _SCHEMA, "root": str(top), "partners": partners}


def load_cochange_history(path) -> dict | None:
    """Load a persisted index, or None on any read/parse error (fail-open)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("partners"), dict):
        return None
    return data


def missing_partners(index, changed_rels) -> list:
    """Omission items for a change set: for each changed file with strong historical
    partners NOT in the change set, one ``{source, partner, co, of, ratio}``.
    Deterministic, zero-LLM; fail-open to [] on a None / malformed index.
    """
    if not isinstance(index, dict):
        return []
    partners = index.get("partners")
    if not isinstance(partners, dict):
        return []
    changed = set(changed_rels or [])
    out = []
    for src in sorted(changed):
        for row in partners.get(src) or []:
            b = row.get("partner")
            if b and b not in changed:
                out.append(
                    {
                        "source": src,
                        "partner": b,
                        "co": row.get("co"),
                        "of": row.get("of"),
                        "ratio": row.get("ratio"),
                    }
                )
    return out
