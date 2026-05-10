"""Verify drift.db survives concurrent writes from multiple hook processes.

Closes the gap I called out: prior tests only exercised concurrent READS
(50 parallel SELECTs). They never proved that 5+ parallel
preflight-and-advise invocations against the same drift.db survive
without lost writes or SQLITE_BUSY exceptions reaching the user.

Round 1: 5 parallel hook subprocesses, same repo. Count rows after.
Round 2: same shape with 10 subprocesses to push harder.

If `busy_timeout` + retry-with-jitter in `drift/sqlite_config.py` are
working, no observation should be lost. If they're broken, you'll see
fewer-than-expected rows in `edit_observations`, OR one of the
subprocesses will exit non-zero.
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PASS, FAIL = [], []
PLUGIN_ROOT = Path("/Users/crisn/Documents/Projects/chameleon")

from _test_config import TS_REPO, require_repo


def t(name, condition, info=""):
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' — ' + info) if info else ''}")


def section(title):
    print(f"\n=== {title} ===")


TS_REPO = require_repo(TS_REPO, "TypeScript")

# Bootstrap + trust to make sure we have a clean profile to observe against.
from chameleon_mcp.tools import bootstrap_repo, trust_profile
if not (TS_REPO / ".chameleon" / "profile.json").is_file():
    bootstrap_repo(str(TS_REPO))
trust_profile(str(TS_REPO), TS_REPO.name)

# Pull N distinct canonical witness paths from the profile so we know each
# resolves to a real archetype (otherwise the hook short-circuits with {}
# and no observation gets recorded).
canonicals = json.loads((TS_REPO / ".chameleon" / "canonicals.json").read_text())
witness_paths = []
for arch_entries in canonicals["canonicals"].values():
    for entry in arch_entries:
        rel = (entry.get("witness") or {}).get("path")
        if rel:
            witness_paths.append(str(TS_REPO / rel))
            break

print(f"Using {len(witness_paths)} canonical witness paths as edit targets")

repo_id = hashlib.sha256(str(TS_REPO.resolve()).encode("utf-8")).hexdigest()


def count_observations(drift_db: Path) -> int:
    if not drift_db.is_file():
        return 0
    conn = sqlite3.connect(str(drift_db), timeout=10.0)
    try:
        return conn.execute("SELECT COUNT(*) FROM edit_observations").fetchone()[0]
    finally:
        conn.close()


def fire_hook(file_path: str, session_id: str) -> tuple[int, str]:
    """Invoke the real preflight-and-advise hook and return (returncode, stderr)."""
    payload = json.dumps({
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path},
        "session_id": session_id,
    })
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
    proc = subprocess.run(
        [str(PLUGIN_ROOT / "hooks" / "preflight-and-advise")],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    return proc.returncode, proc.stderr


def run_concurrent_burst(n: int, label: str) -> None:
    """Spawn n parallel hook invocations against witness paths; report results."""
    section(label)

    # Snapshot row count BEFORE
    import os as _os
    drift_db_root = Path(_os.environ.get("CHAMELEON_PLUGIN_DATA",
                                         str(Path.home() / ".local/share/chameleon")))
    drift_db = drift_db_root / repo_id / "drift.db"
    before = count_observations(drift_db)
    t(f"Baseline row count obtained ({before})", True)

    # Fire n hooks in parallel. Each gets a distinct session_id + cycles
    # through the witness paths.
    started = time.time()
    rcs = []
    stderrs = []
    with ThreadPoolExecutor(max_workers=n) as ex:
        futures = [
            ex.submit(fire_hook,
                      witness_paths[i % len(witness_paths)],
                      f"concurrent-burst-{i}")
            for i in range(n)
        ]
        for f in as_completed(futures):
            rc, err = f.result()
            rcs.append(rc)
            if err:
                stderrs.append(err)
    elapsed = time.time() - started

    t(
        f"All {n} hook subprocesses exited 0 (took {elapsed:.1f}s)",
        all(rc == 0 for rc in rcs),
        f"non-zero rcs: {[r for r in rcs if r != 0]}" if any(r != 0 for r in rcs) else "",
    )

    # Verify no SQLITE_BUSY made it into a subprocess's stderr (the
    # retry-with-jitter should have absorbed transient contention).
    busy_leaks = [e for e in stderrs if "SQLITE_BUSY" in e or "database is locked" in e]
    t(
        f"No SQLITE_BUSY leaked to stderr ({len(busy_leaks)} leaks)",
        not busy_leaks,
        busy_leaks[0][:200] if busy_leaks else "",
    )

    # Count rows AFTER
    after = count_observations(drift_db)
    delta = after - before
    t(
        f"Exactly {n} new observations recorded (got {delta})",
        delta == n,
        f"before={before} after={after}",
    )


# ---------------------------------------------------------------------------
# Round 1 — 5 parallel writers
# ---------------------------------------------------------------------------
run_concurrent_burst(5, "Round 1 — 5 parallel preflight hooks")


# ---------------------------------------------------------------------------
# Round 2 — push harder: 10 parallel writers
# ---------------------------------------------------------------------------
run_concurrent_burst(10, "Round 2 — 10 parallel preflight hooks (stress)")


# ---------------------------------------------------------------------------
# Round 2 — WAL integrity after the burst (no corruption, no stuck WAL)
# ---------------------------------------------------------------------------
section("Round 2 — drift.db integrity after concurrent writes")

drift_db_root = Path(os.environ.get("CHAMELEON_PLUGIN_DATA",
                                     str(Path.home() / ".local/share/chameleon")))
drift_db = drift_db_root / repo_id / "drift.db"

conn = sqlite3.connect(str(drift_db), timeout=10.0)
try:
    ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
    t(f"PRAGMA integrity_check returns 'ok' (got {ok!r})", ok == "ok")

    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    t(f"Still WAL after burst (got {journal_mode})", journal_mode.lower() == "wal")

    # The files table should reflect at least one of our written rel_paths
    sample_rel = str(Path(witness_paths[0]).relative_to(TS_REPO)) if False else witness_paths[0]
    rows = conn.execute(
        "SELECT COUNT(*) FROM files WHERE rel_path = ?", (witness_paths[0],)
    ).fetchone()[0]
    t(
        f"files table upserted for first witness path (got {rows} rows)",
        rows >= 1,
    )
finally:
    conn.close()


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
section("Summary")
print(f"\n  Total: {len(PASS) + len(FAIL)}")
print(f"  Pass: {len(PASS)}")
print(f"  Fail: {len(FAIL)}")
if FAIL:
    print("\n  FAILURES:")
    for name, info in FAIL:
        print(f"    - {name}{(': ' + info) if info else ''}")
    sys.exit(1)
sys.exit(0)
