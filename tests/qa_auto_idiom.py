"""QA battery for the /chameleon-auto-idiom support surface.

Exercises get_idiom_coverage + check_idiom_candidates against two real
profiled repos (TS + Ruby), building the covered/duplicate probes from each
repo's OWN coverage data so the dedup guarantees are tested against real
conventions, not synthetic fixtures.

Read-only against the real repos. The write-path lifecycle (teach -> recheck
-> append-only proof) runs on a temp COPY of each repo's .chameleon, never
the repo itself.

Set CHAMELEON_TEST_TS_REPO and CHAMELEON_TEST_RUBY_REPO to the absolute
paths of profiled repos before running.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

TS_REPO = Path(os.environ.get("CHAMELEON_TEST_TS_REPO", ""))
RUBY_REPO = Path(os.environ.get("CHAMELEON_TEST_RUBY_REPO", ""))

_results: list[tuple[str, bool, str]] = []


def _record(name: str, passed: bool, detail: str = "") -> None:
    tag = "PASS" if passed else "FAIL"
    _results.append((name, passed, detail))
    print(f"  [{tag}] {name}" + (f"  -- {detail}" if detail else ""))


def _summary() -> int:
    total = len(_results)
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = total - passed
    print(f"\n{'=' * 60}")
    print(f"  {passed}/{total} passed, {failed} failed")
    if failed:
        print("\n  Failures:")
        for name, ok, detail in _results:
            if not ok:
                print(f"    - {name}: {detail}")
    print(f"{'=' * 60}")
    return 0 if failed == 0 else 1


def _coverage(repo: Path) -> dict:
    from chameleon_mcp.tools import get_idiom_coverage

    return get_idiom_coverage(str(repo))["data"]


def _check(repo: Path, candidates: list) -> dict:
    from chameleon_mcp.tools import check_idiom_candidates

    return check_idiom_candidates(str(repo), candidates)["data"]


def _snapshot(repo: Path) -> dict:
    out = {}
    for p in sorted((repo / ".chameleon").rglob("*")):
        if p.is_file():
            out[str(p)] = (p.stat().st_mtime_ns, p.stat().st_size)
    return out


NOVEL = {
    "slug": "qa-fictional-quokka-ledger",
    "rationale": (
        "Quokka ledger entries are reconciled through the nightly marsupial "
        "batch; direct ledger writes bypass the reconciliation invariants."
    ),
    "example": "MarsupialBatch.enqueue(entry)",
    "counterexample": "ledger.write(entry)",
}


def battery(label: str, repo: Path) -> None:
    cov = _coverage(repo)

    # Precondition: the battery probes profile-derived coverage, which an
    # untrusted profile correctly withholds (status != "ok", covered == {}).
    # Skip with a clear message instead of crashing on the empty covered map so a
    # human who runs this against an untrusted repo learns to grant trust first.
    if cov.get("status") != "ok" or not isinstance(cov.get("covered"), dict) or not cov["covered"]:
        _record(
            f"{label}_00_precondition",
            False,
            f"coverage status={cov.get('status')!r} (run /chameleon-trust on {repo} first)",
        )
        return

    # 1. Coverage map populated from the real profile.
    ok = (
        cov.get("status") == "ok"
        and cov["covered"]["principles"]
        and cov["covered"]["archetypes"]
        and cov["covered"]["convention_kinds"]
    )
    _record(
        f"{label}_01_coverage_ok",
        bool(ok),
        f"lang={cov.get('language')} principles={len(cov['covered']['principles'])} "
        f"archetypes={len(cov['covered']['archetypes'])}",
    )

    # 2. Both tools are strictly read-only against the real repo.
    before = _snapshot(repo)
    _coverage(repo)
    _check(repo, [dict(NOVEL)])
    _record(f"{label}_02_read_only", _snapshot(repo) == before)

    # 3. A candidate restating the repo's own file-naming convention -> covered.
    naming = cov["covered"]["naming"]
    if naming:
        arch, casing = next(iter(sorted(naming.items())))
        res = _check(
            repo,
            [
                {
                    "slug": "qa-naming-restatement",
                    "rationale": f"All {arch} file names must use {casing} casing.",
                    "archetype": arch,
                }
            ],
        )["results"][0]
        _record(
            f"{label}_03_covered_naming",
            res["verdict"] == "covered" and any("covered-by-naming" in r for r in res["reasons"]),
            f"{arch}/{casing} -> {res['verdict']} {res['reasons']}",
        )
    else:
        _record(f"{label}_03_covered_naming", True, "no naming conventions derived; skipped")

    # 4. A candidate restating an auto-derived principle -> covered.
    principle = max(cov["covered"]["principles"], key=len)
    res = _check(
        repo,
        [{"slug": "qa-principle-restatement", "rationale": principle}],
    )["results"][0]
    _record(
        f"{label}_04_covered_principle",
        res["verdict"] == "covered" and any("covered-by-principle" in r for r in res["reasons"]),
        f"-> {res['verdict']} {res['reasons']}",
    )

    # 5. A candidate restating the dominant base class -> covered (when derived).
    inheritance = cov["covered"]["inheritance"]
    if inheritance:
        arch, data = next(iter(sorted(inheritance.items())))
        base = data.get("dominant_base") or (data.get("known_bases") or [""])[0]
        res = _check(
            repo,
            [
                {
                    "slug": "qa-inheritance-restatement",
                    "rationale": f"Every {arch} class must inherit from {base}.",
                    "archetype": arch,
                }
            ],
        )["results"][0]
        _record(
            f"{label}_05_covered_inheritance",
            res["verdict"] == "covered"
            and any("covered-by-inheritance" in r for r in res["reasons"]),
            f"{base} -> {res['verdict']} {res['reasons']}",
        )
    else:
        _record(f"{label}_05_covered_inheritance", True, "no inheritance derived; skipped")

    # 6. Formatting guidance -> covered by lint rules.
    res = _check(
        repo,
        [
            {
                "slug": "qa-lint-restatement",
                "rationale": "Always use 2-space indentation and never leave a trailing comma.",
            }
        ],
    )["results"][0]
    _record(
        f"{label}_06_covered_lint",
        res["verdict"] == "covered" and "covered-by-lint-rules" in res["reasons"],
        f"-> {res['verdict']} {res['reasons']}",
    )

    # 7. A genuinely novel candidate -> novel, no reasons.
    res = _check(repo, [dict(NOVEL)])["results"][0]
    _record(
        f"{label}_07_novel_passes",
        res["verdict"] == "novel" and res["reasons"] == [],
        f"-> {res['verdict']} {res['reasons']}",
    )

    # 8. Invalid candidates flagged individually, not crashing the batch.
    data = _check(repo, [{"slug": "Bad Slug!", "rationale": "x" * 50}, dict(NOVEL)])
    _record(
        f"{label}_08_invalid_isolated",
        data["results"][0]["verdict"] == "invalid" and data["results"][1]["verdict"] == "novel",
        f"-> {[r['verdict'] for r in data['results']]}",
    )

    # 9. In-batch self-duplication caught.
    reworded = {
        "slug": "qa-fictional-quokka-ledger-two",
        "rationale": (
            "Reconcile quokka ledger entries through the nightly marsupial batch; "
            "writing the ledger directly bypasses reconciliation invariants."
        ),
    }
    data = _check(repo, [dict(NOVEL), reworded])
    _record(
        f"{label}_09_in_batch_dedup",
        data["results"][0]["verdict"] == "novel" and data["results"][1]["verdict"] == "duplicate",
        f"-> {[r['verdict'] for r in data['results']]} {data['results'][1]['reasons']}",
    )

    # 10-13. Write-path lifecycle on a temp COPY of the profile.
    os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
    tmpdir = Path(tempfile.mkdtemp(prefix="qa-auto-idiom-"))
    try:
        from chameleon_mcp.tools import teach_profile_structured

        tmp_repo = tmpdir / "repo"
        tmp_repo.mkdir()
        shutil.copytree(repo / ".chameleon", tmp_repo / ".chameleon")
        # The idiom tools gate on trust; grant it on the fresh tmp copy so the
        # write-path lifecycle exercises the trusted (real-use) path.
        from chameleon_mcp import tools as _t
        from chameleon_mcp.profile.trust import grant_trust as _grant

        _grant(_t._compute_repo_id(tmp_repo), tmp_repo / ".chameleon")

        res = teach_profile_structured(str(tmp_repo), **{k: v for k, v in NOVEL.items()})
        taught = res["data"].get("status") == "success"
        _record(f"{label}_10_teach_novel", taught, str(res["data"])[:120])

        cov2 = _coverage(tmp_repo)
        _record(
            f"{label}_11_coverage_counts_taught",
            cov2["existing_idioms"]["active_count"]
            == _coverage(repo)["existing_idioms"]["active_count"] + 1,
            f"tmp active_count={cov2['existing_idioms']['active_count']}",
        )

        # Re-deriving the same idiom (same slug, or reworded under a new slug)
        # must now be rejected.
        data = _check(tmp_repo, [dict(NOVEL), reworded])
        _record(
            f"{label}_12_recheck_after_teach",
            data["results"][0]["verdict"] == "duplicate"
            and data["results"][1]["verdict"] == "duplicate",
            f"-> {[r['verdict'] for r in data['results']]}",
        )

        # Append-only: teaching a second idiom keeps the first block verbatim.
        first_block = None
        idioms_path = tmp_repo / ".chameleon" / "idioms.md"
        text = idioms_path.read_text(encoding="utf-8")
        start = text.find(f"### {NOVEL['slug']}")
        if start != -1:
            first_block = text[start : start + 200]
        teach_profile_structured(
            str(tmp_repo),
            slug="qa-second-idiom",
            rationale="Second QA idiom to prove appends never rewrite earlier entries.",
        )
        text2 = idioms_path.read_text(encoding="utf-8")
        _record(
            f"{label}_13_append_only",
            bool(first_block) and first_block in text2 and "qa-second-idiom" in text2,
            "first idiom block intact after second teach",
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main() -> int:
    if not os.environ.get("CHAMELEON_TEST_TS_REPO") or not os.environ.get(
        "CHAMELEON_TEST_RUBY_REPO"
    ):
        print("SKIP: CHAMELEON_TEST_TS_REPO and CHAMELEON_TEST_RUBY_REPO not set")
        return 0

    print("=" * 60)
    print("  chameleon auto-idiom QA battery")
    print("=" * 60)

    for label, repo in [("TS", TS_REPO), ("Ruby", RUBY_REPO)]:
        if not (repo / ".chameleon" / "profile.json").is_file():
            print(f"  ABORT: {label} repo has no chameleon profile at {repo}")
            return 1
    print(f"  TS repo:   {TS_REPO}")
    print(f"  Ruby repo: {RUBY_REPO}")

    for label, repo in [("ts", TS_REPO), ("ruby", RUBY_REPO)]:
        print(f"\n-- {label} --")
        battery(label, repo)

    return _summary()


if __name__ == "__main__":
    sys.exit(main())
