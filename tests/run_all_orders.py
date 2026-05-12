"""Test-suite isolation verifier.

Runs all 6 chameleon test files in 4 orders (canonical + 3 randomized). If any
test depends on state set up by an earlier file, at least one ordering
will fail.

Usage (from chameleon repo root):
    cd mcp && PYTHONPATH=. .venv/bin/python ../tests/run_all_orders.py
"""

import random
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
TESTS = [
    TESTS_DIR / "smoke_test.py",
    TESTS_DIR / "comprehensive_test.py",
    TESTS_DIR / "bootstrap_mechanism_test.py",
    TESTS_DIR / "mcp_protocol_test.py",
    TESTS_DIR / "stubs_implemented_test.py",
    TESTS_DIR / "hook_evals" / "runner.py",
]

ORDERS = []
random.seed(42)
for _ in range(3):
    o = TESTS[:]
    random.shuffle(o)
    ORDERS.append(o)
# Always include the canonical order as a baseline
ORDERS.insert(0, TESTS[:])

failures = []

for i, order in enumerate(ORDERS):
    print(f"\n========== Order {i + 1}/{len(ORDERS)} ==========")
    for path in order:
        print(f"\n----- {path.name} -----")
        proc = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(TESTS_DIR.parent / "mcp"),
            env={
                "PYTHONPATH": f"{TESTS_DIR.parent / 'mcp'}:{TESTS_DIR}",
                **__import__("os").environ,
            },
            capture_output=True,
            text=True,
            timeout=600,
        )
        # Print only the summary of each test for brevity
        lines = proc.stdout.splitlines()
        summary_idx = None
        for idx, line in enumerate(lines):
            if "Summary" in line:
                summary_idx = idx
                break
        if summary_idx is not None:
            print("\n".join(lines[summary_idx:]))
        else:
            print(proc.stdout[-500:])
        if proc.returncode != 0:
            print(f"!!! FAILED (rc={proc.returncode}): {path.name} in order {i + 1}")
            print("STDERR tail:", proc.stderr[-500:])
            failures.append((i + 1, path.name))

print("\n========== Order-Independence Summary ==========")
print(f"  Orders tested: {len(ORDERS)}")
print(f"  Files per order: {len(TESTS)}")
print(f"  Total runs: {len(ORDERS) * len(TESTS)}")
print(f"  Failures: {len(failures)}")
if failures:
    for o, name in failures:
        print(f"    - Order {o}: {name}")
    sys.exit(1)
print("  ALL ORDERS PASSED — suite is order-independent.")
sys.exit(0)
