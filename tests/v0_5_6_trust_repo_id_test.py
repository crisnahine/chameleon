"""Regression test for BUG-004: trust_profile accepts repo_id or path.

Pre-v0.5.6, trust_profile required an absolute path and rejected a
repo_id with "repo path must be absolute". Every other MCP tool
(get_archetype, refresh_repo, propose_archetype_renames, ...) already
accepted either.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_6_trust_repo_id_test.py
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_6_trust_repo_id_data_")
os.environ["CHAMELEON_PLUGIN_DATA"] = TMPDATA

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


from chameleon_mcp.tools import bootstrap_repo, trust_profile  # noqa: E402


def main() -> int:
    print("=== BUG-004: trust_profile accepts repo_id or path ===")

    with tempfile.TemporaryDirectory(prefix="bug004_") as td:
        root = Path(td)
        (root / "package.json").write_text(
            '{"name":"x","dependencies":{"typescript":"5"}}'
        )
        (root / "tsconfig.json").write_text("{}")
        (root / "a.ts").write_text("export const x = 1;\n")
        boot = bootstrap_repo(str(root))
        t(
            "bootstrap success",
            boot["data"].get("status") == "success",
            f"got {boot['data']!r}",
        )

        resolved_root = Path(td).resolve()
        repo_id = hashlib.sha256(
            str(resolved_root).encode("utf-8")
        ).hexdigest()
        short = repo_id[:8]

        # Try trust by repo_id (used to fail)
        resp_by_id = trust_profile(repo_id, f"yes-trust-{short}")
        t(
            "trust_profile accepts repo_id",
            resp_by_id["data"].get("status") == "success",
            f"got {resp_by_id['data']!r}",
        )

    # Fresh setup so we can test by-path on a clean repo too
    with tempfile.TemporaryDirectory(prefix="bug004p_") as td:
        root = Path(td)
        (root / "package.json").write_text(
            '{"name":"x","dependencies":{"typescript":"5"}}'
        )
        (root / "tsconfig.json").write_text("{}")
        (root / "a.ts").write_text("export const y = 1;\n")
        bootstrap_repo(str(root))
        resp_by_path = trust_profile(str(root), root.name)
        t(
            "trust_profile still accepts absolute path",
            resp_by_path["data"].get("status") == "success",
            f"got {resp_by_path['data']!r}",
        )

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
