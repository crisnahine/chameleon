"""Real-world effectiveness study — D2: governed vs ungoverned files (same window).

Pre-registered in docs/effectiveness-study.md. The repo-wide before/after arms
(D1, H2) are dominated by concurrent org changes (an org-level review-process
step change swamps H2) and by the fact that only a fraction of post-adoption
changes were chameleon-governed. D2 isolates chameleon by comparing, WITHIN the
same post-adoption weeks, the files chameleon governed (from session
attestations) against the files it did not, on the same repo, with the same lint
engine — so the temporal confound cancels.

Metric: new-violation rate per file (lint_file violations on the file's content
at the production tip). Unit of resampling: the file. Two-sample cluster
bootstrap CI on (ungoverned_rate - governed_rate); a POSITIVE lower bound means
governed files carry fewer violations (an improvement).

Declared limitation (fixed here, not after results): SELECTION BIAS. Governed
files are the ones the chameleon-using developer chose to work on, not a random
sample, so they may differ systematically from ungoverned files independent of
chameleon. D2 removes the temporal confound, not the selection confound; read it
alongside D1/H2, not as a clean causal estimate.

Usage:
    CHAMELEON_TEST_TS_REPO=/abs/ef-client CHAMELEON_TEST_RUBY_REPO=/abs/ef-api \\
      PYTHONPATH=. plugin/mcp/.venv/bin/python tests/study_d2.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.study_analyze import two_sample_boot

from chameleon_mcp.tools import _compute_repo_id, get_archetype, lint_file

_ADOPTION = "2026-06-01"
_DEFAULT_REF = "origin/production"
_SRC_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".rb", ".py")
_MAX_BYTES = 400_000
_MIN_GOVERNED = 12  # below this the arm is reported underpowered, not tested


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True).stdout


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


def _governed_files(data_dir: Path, repo_id: str) -> set[str]:
    f = data_dir / repo_id / "session_attestations.ndjson"
    gov: set[str] = set()
    if not f.is_file():
        return gov
    for line in f.read_text(errors="replace").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if (r.get("ts") or "") < _ADOPTION:
            continue
        for e in r.get("governed_files") or []:
            fp = e.get("file")
            if fp:
                gov.add(fp)
    return gov


def _merged_src_files(repo: Path, ref: str) -> set[str]:
    out = _git(
        repo,
        "log",
        "--first-parent",
        "--since",
        _ADOPTION,
        "--name-only",
        "--format=",
        "--diff-filter=AM",
        ref,
    )
    return {ln for ln in out.splitlines() if ln.strip().lower().endswith(_SRC_EXTS)}


def _blob_at_tip(repo: Path, ref: str, rel: str) -> str | None:
    r = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{rel}"], capture_output=True)
    if r.returncode != 0 or len(r.stdout) > _MAX_BYTES:
        return None
    return r.stdout.decode("utf-8", errors="replace")


def _violations_for(repo: Path, rid: str, ref: str, rel: str) -> int | None:
    content = _blob_at_tip(repo, ref, rel)
    if content is None:
        return None
    try:
        arch = (get_archetype(rid, str(repo / rel)).get("data") or {}).get("archetype") or "none"
        out = lint_file(rid, arch, content, str(repo / rel)).get("data") or {}
        return len(out.get("violations") or [])
    except Exception:
        return None


def measure(repo_path: str, data_dir: Path) -> dict:
    repo = Path(repo_path)
    rid = _compute_repo_id(repo)
    ref = _resolve_ref(repo)
    governed = _governed_files(data_dir, rid)
    merged = _merged_src_files(repo, ref)
    gov_merged = sorted(governed & merged)
    ungov_merged = sorted(merged - governed)

    def arm_units(files):
        units = []
        for rel in files:
            v = _violations_for(repo, rid, ref, rel)
            if v is not None:
                units.append((v, 1.0))  # violations per file
        return units

    gov_units = arm_units(gov_merged)
    ungov_units = arm_units(ungov_merged)
    # (ungoverned - governed): positive lower bound => governed has fewer violations
    res = two_sample_boot(ungov_units, gov_units)
    underpowered = len(gov_units) < _MIN_GOVERNED
    return {
        "repo": str(repo),
        "ref": ref,
        "governed_merged_files": len(gov_merged),
        "ungoverned_merged_files": len(ungov_merged),
        "governed_scored": len(gov_units),
        "ungoverned_scored": len(ungov_units),
        "ungoverned_viol_per_file": res["pre_rate"] / 100 if res["pre_rate"] is not None else None,
        "governed_viol_per_file": res["post_rate"] / 100 if res["post_rate"] is not None else None,
        "diff_per_file": res["diff"] / 100 if res["diff"] is not None else None,
        "ci_lo": res["lo"] / 100 if res["lo"] is not None else None,
        "ci_hi": res["hi"] / 100 if res["hi"] is not None else None,
        "underpowered": underpowered,
    }


def main() -> int:
    data_dir = Path(
        os.environ.get("CHAMELEON_PLUGIN_DATA") or (Path.home() / ".local/share/chameleon")
    )
    targets = [
        ("ef-client (TS)", os.environ.get("CHAMELEON_TEST_TS_REPO")),
        ("ef-api (Rails)", os.environ.get("CHAMELEON_TEST_RUBY_REPO")),
    ]
    results = []
    for label, path in targets:
        if not path or not (Path(path) / ".chameleon" / "profile.json").is_file():
            print(f"SKIP {label}: no repo / no committed profile", file=sys.stderr)
            continue
        print(f"=== D2 governed vs ungoverned: {label} ===", file=sys.stderr)
        r = measure(path, data_dir)
        r["label"] = label
        results.append(r)
        note = "  [UNDERPOWERED]" if r["underpowered"] else ""
        print(
            f"  governed: {r['governed_viol_per_file']} viol/file (n={r['governed_scored']}){note}",
            file=sys.stderr,
        )
        print(
            f"  ungoverned: {r['ungoverned_viol_per_file']} viol/file (n={r['ungoverned_scored']})",
            file=sys.stderr,
        )
        if r["diff_per_file"] is not None:
            print(
                f"  diff (ungov - gov): {r['diff_per_file']} 95%CI[{r['ci_lo']}, {r['ci_hi']}]",
                file=sys.stderr,
            )
    print(json.dumps({"study": "D2_governed_vs_ungoverned", "results": results}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
