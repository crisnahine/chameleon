# SLOW: 1-3 min
"""50,000-file bootstrap scale-validation test.

Two scenarios run back-to-back against synthetic TS repos generated in
temp directories:

  A. Ceiling trip — 50,001 files → bootstrap_repo returns
     status='failed_too_many_files' (REPO_SIZE_GUARD enforced).

  B. At-cap bootstrap — exactly 50,000 files → bootstrap_repo succeeds.
     Wall-clock and peak RSS are measured via time.monotonic() and
     resource.getrusage(RUSAGE_SELF). Assertions:
       * duration < 60s
       * peak RSS delta < 2 GB
     Actual numbers are echoed to stdout so future regressions are
     catchable by `grep`.

  C. Post-bootstrap query — 10 random files in the at-cap fixture are
     resolved via get_pattern_context. Each must return within 2s and
     surface a non-null archetype name (the at-cap fixture has clear,
     dense clusters so the bootstrap MUST have produced archetypes).

Skip semantics:
  Set CHAMELEON_SKIP_SLOW_TESTS=1 to skip everything. The file then
  exits 0 with a SKIP marker so the broader test runner stays green.

Footprint:
  Each on-disk file is ~200 B of minimal TS so the 50,001-file fixture
  weighs in around ~10 MB on-disk. Both fixtures are cleaned up in a
  try/finally so a crashed test leaves nothing behind in /tmp.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/stress_50k_test.py
"""

from __future__ import annotations

import json
import os
import random
import resource
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Make the in-repo chameleon_mcp importable without installing.
HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

PASS = 0
FAIL = 0


def t(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


def section(name: str) -> None:
    print(f"\n=== {name} ===")


# ---------------------------------------------------------------------------
# Skip gate: CHAMELEON_SKIP_SLOW_TESTS=1 short-circuits the file.
# ---------------------------------------------------------------------------
if os.environ.get("CHAMELEON_SKIP_SLOW_TESTS", "").strip() == "1":
    print("SKIP: CHAMELEON_SKIP_SLOW_TESTS=1 (50k bootstrap takes 1-3 minutes)")
    sys.exit(0)


# Use isolated plugin data dir per run so the index.db + drift.db this
# bootstrap writes don't poison the user's real install.
_TMP_DATA = tempfile.mkdtemp(prefix="chameleon_stress_50k_data_")
os.environ["CHAMELEON_PLUGIN_DATA"] = _TMP_DATA

from chameleon_mcp.bootstrap.discovery import (  # noqa: E402
    REPO_SIZE_GUARD,
    discover_files,
)
from chameleon_mcp.tools import (  # noqa: E402
    bootstrap_repo as bootstrap_repo_tool,
)
from chameleon_mcp.tools import (
    get_pattern_context,
)

# Sanity-check the constant the fixture is built around — if the codebase
# ever moves the ceiling, this test should fail loudly instead of silently
# building a too-small fixture.
assert REPO_SIZE_GUARD == 50_000, (
    f"This test is hardcoded around REPO_SIZE_GUARD=50000; "
    f"got {REPO_SIZE_GUARD}. Update the fixture sizes if the ceiling moved."
)


# ---------------------------------------------------------------------------
# Fixture generation
# ---------------------------------------------------------------------------

# Distribute files across 10 archetype-shaped folders so clustering has
# something realistic to chew on. The folder names mimic real TS repos.
_ARCHETYPE_DIRS = (
    "src/components",
    "src/queries",
    "src/utils",
    "src/services",
    "src/hooks",
    "src/api",
    "src/models",
    "src/middleware",
    "src/repositories",
    "src/controllers",
)


# Per-archetype minimal source templates (~200 B each post-format). The
# top-level node shape + content-signal tokens vary so the clustering
# layer produces ≥10 archetypes instead of collapsing into one mega-bucket.
_TEMPLATES = {
    "src/components": (
        "import React from 'react';\n"
        "export const Component{i} = (props: {{ value: number }}) => {{\n"
        "  return <div className='c{i}'>{{props.value}}</div>;\n"
        "}};\n"
    ),
    "src/queries": (
        "import {{ useQuery }} from 'react-query';\n"
        "export const useQuery{i} = (id: string) =>\n"
        "  useQuery(['q{i}', id], async () => fetch(`/api/{i}/${{id}}`));\n"
    ),
    "src/utils": (
        "export const util{i} = (x: number): number => x + {i};\n"
        "export const helper{i} = (s: string): string => s + '{i}';\n"
    ),
    "src/services": (
        "export class Service{i} {{\n"
        "  async run(input: string): Promise<number> {{\n"
        "    return input.length + {i};\n"
        "  }}\n"
        "}}\n"
    ),
    "src/hooks": (
        "import {{ useState, useEffect }} from 'react';\n"
        "export function useHook{i}(initial: number) {{\n"
        "  const [v, setV] = useState(initial + {i});\n"
        "  useEffect(() => {{ setV(v + 1); }}, []);\n"
        "  return v;\n"
        "}}\n"
    ),
    "src/api": (
        "export async function fetchApi{i}(id: string) {{\n"
        "  const r = await fetch(`/api/v{i}/${{id}}`);\n"
        "  return r.json();\n"
        "}}\n"
    ),
    "src/models": (
        "export interface Model{i} {{\n"
        "  id: string;\n"
        "  name: string;\n"
        "  value{i}: number;\n"
        "}}\n"
    ),
    "src/middleware": (
        "export const middleware{i} = (req: any, res: any, next: any) => {{\n"
        "  req.meta{i} = {i};\n"
        "  next();\n"
        "}};\n"
    ),
    "src/repositories": (
        "export class Repository{i} {{\n"
        "  async find(id: string) {{\n"
        "    return {{ id, value: {i} }};\n"
        "  }}\n"
        "}}\n"
    ),
    "src/controllers": (
        "export class Controller{i} {{\n"
        "  async handle(req: any) {{\n"
        "    return {{ status: 200, body: {i} }};\n"
        "  }}\n"
        "}}\n"
    ),
}


def make_synthetic_repo(root: Path, n_files: int) -> list[Path]:
    """Create a TS repo with `n_files` source files distributed across 10
    archetype-shaped folders.

    Returns the list of created file paths (absolute) so the caller can
    sample from it without re-walking the tree.
    """
    root.mkdir(parents=True, exist_ok=True)

    # Required for TS extractor detection.
    (root / "tsconfig.json").write_text(
        json.dumps({"compilerOptions": {"strict": True, "target": "ESNext"}}),
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        json.dumps({
            "name": "stress-50k",
            "dependencies": {"typescript": "5.0.0", "react": "18.0.0"},
        }),
        encoding="utf-8",
    )

    # Pre-make all 10 archetype directories. mkdir(parents=True) per file
    # would dominate setup time; one shot per directory is much cheaper.
    for d in _ARCHETYPE_DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)

    created: list[Path] = []
    for i in range(n_files):
        bucket = _ARCHETYPE_DIRS[i % len(_ARCHETYPE_DIRS)]
        template = _TEMPLATES[bucket]
        ext = ".tsx" if bucket == "src/components" else ".ts"
        # Use a per-bucket sequential name so file paths are deterministic
        # and the same template generates the same body across re-runs.
        path = root / bucket / f"file_{i:06d}{ext}"
        path.write_text(template.format(i=i), encoding="utf-8")
        created.append(path)
    return created


def _rss_bytes() -> int:
    """Return the current process's peak resident-set size in bytes.

    `resource.ru_maxrss` reports bytes on macOS and kilobytes on Linux —
    normalize to bytes here so the test reports the same unit on both.
    """
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "linux":
        return raw * 1024
    return raw


# ---------------------------------------------------------------------------
# Scenario A — Ceiling trip at 50,001 files
# ---------------------------------------------------------------------------
section(f"A. Ceiling trip — {REPO_SIZE_GUARD + 1} files should fail")

over_cap = Path(tempfile.mkdtemp(prefix="chameleon_stress_50k_over_"))
try:
    setup_start = time.monotonic()
    files = make_synthetic_repo(over_cap, REPO_SIZE_GUARD + 1)
    setup_elapsed = time.monotonic() - setup_start
    print(
        f"    fixture: {len(files)} files in {setup_elapsed:.1f}s "
        f"({over_cap})"
    )

    # Sanity: discover_files raises directly when over the ceiling, so
    # we don't pay the full pipeline cost.
    try:
        discover_files(over_cap)
        # If we reach here, ceiling didn't trip — record a failure but
        # keep going so the assertion via bootstrap_repo also runs.
        t("discover_files raises TooManyFilesError at 50,001", False)
    except Exception as e:
        t(
            "discover_files raises TooManyFilesError at 50,001",
            type(e).__name__ == "TooManyFilesError",
            f"got {type(e).__name__}",
        )

    # End-to-end: bootstrap_repo translates the exception into a
    # failed_too_many_files envelope.
    bootstrap_start = time.monotonic()
    result = bootstrap_repo_tool(str(over_cap))
    bootstrap_elapsed = time.monotonic() - bootstrap_start
    data = result["data"]
    print(
        f"    bootstrap_repo: status={data['status']!r} "
        f"in {bootstrap_elapsed:.1f}s"
    )
    t(
        f"bootstrap_repo returns failed_too_many_files "
        f"(got {data['status']!r})",
        data["status"] == "failed_too_many_files",
        data.get("error", "") or "",
    )
    t(
        "error message mentions ceiling",
        "ceiling" in (data.get("error") or "").lower()
        or str(REPO_SIZE_GUARD) in (data.get("error") or ""),
        data.get("error", ""),
    )
    t(
        "no profile dir created on ceiling trip",
        not (over_cap / ".chameleon").exists(),
    )
finally:
    shutil.rmtree(over_cap, ignore_errors=True)


# ---------------------------------------------------------------------------
# Scenario B — Bootstrap succeeds at exactly 50,000 files
# ---------------------------------------------------------------------------
section(f"B. At-cap bootstrap — exactly {REPO_SIZE_GUARD} files should succeed")

# Measurable wall-clock + peak memory. Capture pre-bootstrap RSS so the
# fixture-generation cost doesn't pollute the bootstrap delta.
at_cap = Path(tempfile.mkdtemp(prefix="chameleon_stress_50k_atcap_"))
duration_s: float | None = None
peak_rss_bytes: int | None = None
bootstrap_result: dict | None = None
created_files: list[Path] = []

try:
    setup_start = time.monotonic()
    created_files = make_synthetic_repo(at_cap, REPO_SIZE_GUARD)
    setup_elapsed = time.monotonic() - setup_start
    print(
        f"    fixture: {len(created_files)} files in {setup_elapsed:.1f}s "
        f"({at_cap})"
    )

    # Sanity: discovery returns exactly 50_000 (no over-the-ceiling raise).
    discovered = discover_files(at_cap)
    print(f"    discover_files: {len(discovered)} files (ceiling = {REPO_SIZE_GUARD})")
    t(
        f"discover_files returns exactly {REPO_SIZE_GUARD}",
        len(discovered) == REPO_SIZE_GUARD,
        f"got {len(discovered)}",
    )

    pre_rss = _rss_bytes()
    bootstrap_start = time.monotonic()
    bootstrap_result = bootstrap_repo_tool(str(at_cap))
    duration_s = time.monotonic() - bootstrap_start
    post_rss = _rss_bytes()
    peak_rss_bytes = max(0, post_rss - pre_rss)

    # Echo the headline numbers BEFORE assertions so a fail still surfaces
    # the measured cost in the test output (grep-able by future operators).
    print(
        f"\n    [MEASURED] 50k-bootstrap duration: {duration_s:.2f}s, "
        f"peak RSS delta: {peak_rss_bytes / 1024 / 1024:.1f} MiB "
        f"(pre={pre_rss / 1024 / 1024:.1f} MiB, "
        f"post={post_rss / 1024 / 1024:.1f} MiB)"
    )

    data = bootstrap_result["data"]
    print(
        f"    bootstrap status={data['status']!r}, "
        f"archetypes={data['archetypes_detected']}, "
        f"files_processed={data['files_processed']}, "
        f"duration_ms={data['duration_ms']}"
    )

    t(
        f"bootstrap_repo returns success (got {data['status']!r})",
        data["status"] == "success",
        data.get("error", "") or "",
    )
    t(
        f"duration < 60s (got {duration_s:.2f}s)",
        duration_s < 60.0,
        f"slow bootstrap: {duration_s:.2f}s",
    )
    # 2 GB ceiling (binary: 2 * 2**30 bytes). Memory delta because the
    # interpreter itself starts at non-trivial RSS in the test process,
    # and we care about the *bootstrap's* growth above that floor.
    two_gb = 2 * (1024**3)
    t(
        f"peak RSS delta < 2 GiB (got {peak_rss_bytes / 1024 / 1024:.1f} MiB)",
        peak_rss_bytes < two_gb,
        f"oom risk: {peak_rss_bytes / 1024 / 1024:.1f} MiB",
    )
    t(
        f"archetypes_detected >= 5 (got {data['archetypes_detected']})",
        data["archetypes_detected"] >= 5,
        "10 templates should cluster into many archetypes",
    )
    t(
        f"files_processed close to {REPO_SIZE_GUARD} "
        f"(got {data['files_processed']})",
        data["files_processed"] >= REPO_SIZE_GUARD * 0.95,
        f"too many files dropped: {data['files_processed']}",
    )
    t(
        ".chameleon/profile.json exists after success",
        (at_cap / ".chameleon" / "profile.json").is_file(),
    )

    # -------------------------------------------------------------------
    # Scenario C — query 10 random files
    # -------------------------------------------------------------------
    if data["status"] == "success":
        section("C. get_pattern_context on 10 random files")

        # Trust the freshly-bootstrapped profile so get_pattern_context
        # surfaces archetype data instead of returning 'untrusted' shape.
        from chameleon_mcp.tools import trust_profile

        trust_profile(str(at_cap), at_cap.name)

        rng = random.Random(0xC0DE)  # deterministic sample for reproducibility
        sample = rng.sample(created_files, 10)
        archetype_hits = 0
        slowest_call = 0.0
        per_call_times: list[float] = []
        # The get_pattern_context envelope shape is
        # data.archetype.archetype (string or None) — same field name
        # returned by get_archetype, which get_pattern_context delegates to.
        for sf in sample:
            call_start = time.monotonic()
            envelope = get_pattern_context(str(sf))
            call_elapsed = time.monotonic() - call_start
            per_call_times.append(call_elapsed)
            slowest_call = max(slowest_call, call_elapsed)
            arch = (envelope.get("data") or {}).get("archetype") or {}
            if arch.get("archetype"):
                archetype_hits += 1
        avg = sum(per_call_times) / len(per_call_times)
        print(
            f"    [MEASURED] get_pattern_context x10: "
            f"avg={avg * 1000:.0f}ms, max={slowest_call * 1000:.0f}ms, "
            f"hits={archetype_hits}/10"
        )
        t(
            f"all 10 calls under 2s each (slowest={slowest_call:.2f}s)",
            slowest_call < 2.0,
            f"slow query: {slowest_call:.2f}s",
        )
        t(
            f"non-null archetype on all 10 files (got {archetype_hits}/10)",
            archetype_hits == 10,
            "trust state may have desynced or clustering missed",
        )
finally:
    shutil.rmtree(at_cap, ignore_errors=True)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
section("Summary")
print(f"\n  Total: {PASS + FAIL}")
print(f"  Pass:  {PASS}")
print(f"  Fail:  {FAIL}")

# Leave a final, grep-friendly headline so a CI log captures the measured
# numbers even if the upstream summarizer truncates.
if duration_s is not None and peak_rss_bytes is not None:
    print(
        f"\n[STRESS-50K-RESULT] duration={duration_s:.2f}s "
        f"rss_delta_mib={peak_rss_bytes / 1024 / 1024:.1f}"
    )

# Best-effort plugin-data cleanup so the host filesystem stays tidy.
shutil.rmtree(_TMP_DATA, ignore_errors=True)

sys.exit(0 if FAIL == 0 else 1)
