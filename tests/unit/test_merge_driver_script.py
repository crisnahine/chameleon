"""The git merge driver must pass file paths to Python safely.

``scripts/chameleon-merge-driver.sh`` previously interpolated the BASE/OURS/
THEIRS paths straight into a ``python -c`` string literal. A path containing a
single quote broke the literal (SyntaxError, the merge aborted) and was a code
injection sink. The driver must merge correctly even when a path contains a
single quote, with no Python traceback.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DRIVER = REPO_ROOT / "scripts" / "chameleon-merge-driver.sh"


def _archetypes(gen: int, names: list[str]) -> str:
    return json.dumps(
        {
            "schema_version": 2,
            "repo_id": "r",
            "generation": gen,
            "archetypes": {
                n: {"cluster_size": 3, "canonical_witness": f"{n}.ts", "summary": n} for n in names
            },
        }
    )


@pytest.mark.skipif(not DRIVER.exists(), reason="merge driver script missing")
def test_merge_driver_handles_single_quote_in_path(tmp_path):
    work = tmp_path / "path with 'quote'"
    work.mkdir(parents=True)
    base = work / "base.json"
    ours = work / "ours.json"
    theirs = work / "theirs.json"
    base.write_text(_archetypes(1, []))
    ours.write_text(_archetypes(2, ["foo"]))
    theirs.write_text(_archetypes(2, ["bar"]))

    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT)
    proc = subprocess.run(
        [str(DRIVER), str(base), str(ours), str(theirs)],
        capture_output=True,
        text=True,
        env=env,
    )

    assert "SyntaxError" not in proc.stderr
    assert "Traceback" not in proc.stderr
    assert proc.returncode == 0, proc.stderr
    merged = json.loads(ours.read_text())
    assert set(merged["archetypes"]) == {"foo", "bar"}
