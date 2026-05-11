"""chameleon calibration harness.

Reads `tests/calibration/corpus.json` (gitignored), runs bootstrap +
sampled `get_pattern_context` calls per repo, and prints a calibration
table.

When `corpus.json` is missing, exits 0 with a "no corpus configured"
row per metric so CI stays green.

See `tests/calibration/README.md` for the full design.
"""
from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "mcp"))


def _emit(payload: dict) -> int:
    print(json.dumps(payload, indent=2))
    return 0


def _no_corpus() -> int:
    return _emit(
        {
            "status": "no_corpus_configured",
            "reason": (
                "tests/calibration/corpus.json is missing. See "
                "tests/calibration/README.md for the schema."
            ),
            "metrics": {
                "archetype_match_rate": "N/A",
                "high_confidence_rate": "N/A",
                "bootstrap_duration_p50_ms": "N/A",
                "bootstrap_duration_p95_ms": "N/A",
                "cost_per_bootstrap_usd": 0.0,
            },
        }
    )


def _load_corpus() -> dict | None:
    config_path = HERE.parent / "corpus.json"
    if not config_path.is_file():
        return None
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _percentile(values: list[float], p: float) -> float:
    """Naive percentile (no scipy). p in [0, 100]."""
    if not values:
        return 0.0
    s = sorted(values)
    if p <= 0:
        return s[0]
    if p >= 100:
        return s[-1]
    idx = int(round((p / 100.0) * (len(s) - 1)))
    return s[idx]


def _run_repo(spec: dict) -> dict:
    from chameleon_mcp.tools import bootstrap_repo, get_pattern_context, trust_profile

    repo_path = Path(spec["path"]).expanduser().resolve()
    if not repo_path.is_dir():
        return {
            "name": spec.get("name", repo_path.name),
            "status": "skipped",
            "reason": f"path is not a directory: {repo_path}",
        }

    started = time.time()
    report = bootstrap_repo(str(repo_path))["data"]
    bootstrap_ms = int((time.time() - started) * 1000)

    if report.get("status") != "success":
        return {
            "name": spec.get("name", repo_path.name),
            "status": "bootstrap_failed",
            "report": report,
        }

    trust_profile(str(repo_path), repo_path.name)

    # Sample one file per archetype from the canonicals.
    canonicals_path = repo_path / ".chameleon" / "canonicals.json"
    canonicals = json.loads(canonicals_path.read_text(encoding="utf-8"))
    sample_files: list[Path] = []
    for archetype_name, entries in canonicals.get("canonicals", {}).items():
        if not entries:
            continue
        witness_rel = entries[0].get("witness", {}).get("path")
        if witness_rel:
            sample_files.append(repo_path / witness_rel)

    # Cap sample size at 100 for speed.
    if len(sample_files) > 100:
        random.seed(42)  # deterministic sampling
        sample_files = random.sample(sample_files, 100)

    matched = 0
    high_confidence = 0
    for f in sample_files:
        r = get_pattern_context(str(f))["data"]
        arch = (r.get("archetype") or {}).get("archetype")
        if arch:
            matched += 1
            band = r.get("archetype", {}).get("confidence_band", "low")
            if band in ("high", "medium"):
                high_confidence += 1

    n = max(1, len(sample_files))
    return {
        "name": spec.get("name", repo_path.name),
        "status": "ok",
        "archetypes_detected": report.get("archetypes_detected"),
        "files_processed": report.get("files_processed"),
        "bootstrap_ms": bootstrap_ms,
        "samples": len(sample_files),
        "archetype_match_rate": matched / n,
        "high_confidence_rate": high_confidence / n,
    }


def main() -> int:
    corpus = _load_corpus()
    if corpus is None or not corpus.get("repos"):
        return _no_corpus()

    rows: list[dict[str, Any]] = []
    for spec in corpus["repos"]:
        rows.append(_run_repo(spec))

    durations = [r["bootstrap_ms"] for r in rows if r.get("status") == "ok"]
    match_rates = [r["archetype_match_rate"] for r in rows if r.get("status") == "ok"]
    hc_rates = [r["high_confidence_rate"] for r in rows if r.get("status") == "ok"]

    return _emit(
        {
            "status": "ok" if match_rates else "no_successful_repos",
            "rows": rows,
            "rollup": {
                "repos_ok": len(durations),
                "archetype_match_rate_mean": (
                    sum(match_rates) / len(match_rates) if match_rates else None
                ),
                "high_confidence_rate_mean": (
                    sum(hc_rates) / len(hc_rates) if hc_rates else None
                ),
                "bootstrap_duration_p50_ms": (
                    _percentile(durations, 50) if durations else None
                ),
                "bootstrap_duration_p95_ms": (
                    _percentile(durations, 95) if durations else None
                ),
                "cost_per_bootstrap_usd": 0.0,
            },
            "targets": {
                "archetype_match_rate_mean": 0.80,
                "bootstrap_duration_p95_ms": 10_000,
            },
        }
    )


if __name__ == "__main__":
    sys.exit(main())
