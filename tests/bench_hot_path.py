"""Benchmark the hot-path latency of chameleon's get_pattern_context.

Measures individual sub-components:
  1. find_repo_root      - directory walk to locate repo marker
  2. _compute_repo_id    - git remote URL fetch + SHA-256
  3. load_profile_dir    - read + parse 4 JSON artifacts + idioms.md
  4. get_pattern_context - full collapsed call (archetype + canonical + rules)

Each is run N times. Reports cold (first call, caches cleared) and warm
(subsequent calls, caches populated) at p50 and p99.

Usage:
    PYTHONPATH=. mcp/.venv/bin/python tests/bench_hot_path.py
"""

from __future__ import annotations

import os
import time
from pathlib import Path


ITERATIONS = 100

_CANDIDATE_REPOS = [
    Path(p)
    for p in (
        os.environ.get("CHAMELEON_TEST_TS_REPO", ""),
        os.environ.get("CHAMELEON_TEST_RUBY_REPO", ""),
    )
    if p
] + [
    Path(os.path.expanduser(f"~/Documents/Projects/Testing Apps/{name}"))
    for name in ("excalidraw", "plane", "bulletproof-react", "maybe", "forem")
]

_CHAMELEON_REPO = Path(__file__).resolve().parent.parent


def _find_profiled_repo() -> Path | None:
    for p in _CANDIDATE_REPOS:
        if (p / ".chameleon" / "profile.json").is_file():
            return p
    return None


def percentile(data: list[float], pct: int) -> float:
    """Return the pct-th percentile of data (0-100)."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * pct / 100
    f = int(k)
    c = f + 1
    if c >= len(sorted_data):
        return sorted_data[f]
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])


def fmt_ms(val: float) -> str:
    """Format seconds as milliseconds with 2 decimal places."""
    return f"{val * 1000:.2f}ms"


def run_bench(name: str, fn, iterations: int = ITERATIONS, clear_fn=None):
    """Run fn() `iterations` times. Return (cold_times, warm_times).

    cold_times: single-element list from the first call after clear_fn().
    warm_times: remaining iterations (caches populated).
    """
    if clear_fn:
        clear_fn()
    t0 = time.perf_counter()
    fn()
    t1 = time.perf_counter()
    cold_times = [t1 - t0]

    warm_times = []
    for _ in range(iterations - 1):
        t0 = time.perf_counter()
        fn()
        t1 = time.perf_counter()
        warm_times.append(t1 - t0)

    return cold_times, warm_times


def print_row(name: str, cold_times: list[float], warm_times: list[float]):
    """Print a single benchmark row."""
    cold_p50 = fmt_ms(percentile(cold_times, 50))
    cold_p99 = fmt_ms(percentile(cold_times, 99))
    if warm_times:
        warm_p50 = fmt_ms(percentile(warm_times, 50))
        warm_p99 = fmt_ms(percentile(warm_times, 99))
    else:
        warm_p50 = "n/a"
        warm_p99 = "n/a"
    print(f"  {name:<40s}  {cold_p50:>10s}  {cold_p99:>10s}  {warm_p50:>10s}  {warm_p99:>10s}")


def main():
    from chameleon_mcp.profile.loader import (
        _PROFILE_CACHE,
        _REPO_ROOT_CACHE,
        find_repo_root,
        load_profile_dir,
    )
    from chameleon_mcp.tools import (
        _REPO_ID_CACHE,
        _compute_repo_id,
        _effective_profile_dir,
        get_pattern_context,
    )

    profiled_repo = _find_profiled_repo()
    chameleon_repo = _CHAMELEON_REPO

    print("=" * 100)
    print("chameleon hot-path benchmark")
    print("=" * 100)
    print()
    print(f"  Chameleon repo (no profile):  {chameleon_repo}")
    if profiled_repo:
        lang_file = profiled_repo / ".chameleon" / "profile.json"
        import json
        lang = "?"
        try:
            with open(lang_file) as f:
                lang = json.load(f).get("language", "?")
        except Exception:
            pass
        print(f"  Profiled repo ({lang}):         {profiled_repo}")
    else:
        print("  Profiled repo:                 (none found)")
    print(f"  Iterations:                    {ITERATIONS}")
    print()

    target_file: str | None = None
    if profiled_repo:
        for ext in ("tsx", "ts", "rb"):
            candidates = list(profiled_repo.glob(f"**/*.{ext}"))
            candidates = [c for c in candidates if "node_modules" not in str(c)]
            if candidates:
                target_file = str(candidates[0])
                break
        if not target_file:
            for f in profiled_repo.rglob("*"):
                if f.is_file() and "node_modules" not in str(f) and ".chameleon" not in str(f):
                    target_file = str(f)
                    break

    if target_file:
        print(f"  Target file:                   {target_file}")
    print()

    chameleon_file = str(chameleon_repo / "mcp" / "chameleon_mcp" / "tools.py")

    def clear_all_caches():
        _REPO_ROOT_CACHE.clear()
        _REPO_ID_CACHE.clear()
        _PROFILE_CACHE.clear()
        from chameleon_mcp import _excerpt_cache
        _excerpt_cache.clear()

    print(f"  {'Component':<40s}  {'Cold p50':>10s}  {'Cold p99':>10s}  {'Warm p50':>10s}  {'Warm p99':>10s}")
    print(f"  {'-' * 40}  {'-' * 10}  {'-' * 10}  {'-' * 10}  {'-' * 10}")

    def clear_repo_root():
        _REPO_ROOT_CACHE.clear()

    cold, warm = run_bench(
        "find_repo_root (no profile)",
        lambda: find_repo_root(Path(chameleon_file)),
        clear_fn=clear_repo_root,
    )
    print_row("find_repo_root (no profile)", cold, warm)

    if target_file:
        cold, warm = run_bench(
            "find_repo_root (profiled)",
            lambda: find_repo_root(Path(target_file)),
            clear_fn=clear_repo_root,
        )
        print_row("find_repo_root (profiled)", cold, warm)

    def clear_repo_id():
        _REPO_ID_CACHE.clear()

    cold, warm = run_bench(
        "_compute_repo_id (chameleon)",
        lambda: _compute_repo_id(chameleon_repo),
        clear_fn=clear_repo_id,
    )
    print_row("_compute_repo_id (chameleon)", cold, warm)

    if profiled_repo:
        cold, warm = run_bench(
            "_compute_repo_id (profiled)",
            lambda: _compute_repo_id(profiled_repo),
            clear_fn=clear_repo_id,
        )
        print_row("_compute_repo_id (profiled)", cold, warm)

    if profiled_repo:
        _REPO_ID_CACHE.clear()
        cold, warm = run_bench(
            "_effective_profile_dir",
            lambda: _effective_profile_dir(profiled_repo),
            clear_fn=lambda: _REPO_ID_CACHE.clear(),
        )
        print_row("_effective_profile_dir", cold, warm)

    if profiled_repo:
        profile_dir = profiled_repo / ".chameleon"

        def clear_profile():
            _PROFILE_CACHE.clear()

        cold, warm = run_bench(
            "load_profile_dir (cold=read, warm=mtime)",
            lambda: load_profile_dir(profile_dir),
            clear_fn=clear_profile,
        )
        print_row("load_profile_dir", cold, warm)

    if target_file:
        cold, warm = run_bench(
            "get_pattern_context (profiled)",
            lambda: get_pattern_context(target_file),
            clear_fn=clear_all_caches,
        )
        print_row("get_pattern_context (profiled)", cold, warm)

    cold, warm = run_bench(
        "get_pattern_context (no profile)",
        lambda: get_pattern_context(chameleon_file),
        clear_fn=clear_all_caches,
    )
    print_row("get_pattern_context (no profile)", cold, warm)

    if target_file:
        print()
        print(f"  {'--- Multi-cold (30 runs, cache cleared each time) ---':<40s}")
        print(f"  {'Component':<40s}  {'p50':>10s}  {'p99':>10s}  {'min':>10s}  {'max':>10s}")
        print(f"  {'-' * 40}  {'-' * 10}  {'-' * 10}  {'-' * 10}  {'-' * 10}")

        cold_times = []
        for _ in range(30):
            clear_all_caches()
            t0 = time.perf_counter()
            get_pattern_context(target_file)
            t1 = time.perf_counter()
            cold_times.append(t1 - t0)

        p50 = fmt_ms(percentile(cold_times, 50))
        p99 = fmt_ms(percentile(cold_times, 99))
        mn = fmt_ms(min(cold_times))
        mx = fmt_ms(max(cold_times))
        print(f"  {'get_pattern_context (cold x30)':<40s}  {p50:>10s}  {p99:>10s}  {mn:>10s}  {mx:>10s}")

        cold_times_noprof = []
        for _ in range(30):
            clear_all_caches()
            t0 = time.perf_counter()
            get_pattern_context(chameleon_file)
            t1 = time.perf_counter()
            cold_times_noprof.append(t1 - t0)

        p50 = fmt_ms(percentile(cold_times_noprof, 50))
        p99 = fmt_ms(percentile(cold_times_noprof, 99))
        mn = fmt_ms(min(cold_times_noprof))
        mx = fmt_ms(max(cold_times_noprof))
        print(f"  {'get_pattern_context no-prof (cold x30)':<40s}  {p50:>10s}  {p99:>10s}  {mn:>10s}  {mx:>10s}")

    print()
    print("=" * 100)


if __name__ == "__main__":
    main()
