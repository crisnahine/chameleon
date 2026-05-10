"""Verify material-change re-prompt flow.

When a teammate updates .chameleon/ and commits, the trusted
profile_sha256 stops matching the new profile.json. The user should
see trust_state="stale" and be prompted to re-confirm via
/chameleon-trust before chameleon resumes injection.

Round 1 — direct API: detect_repo + get_pattern_context return
          "stale" after the profile is mutated.
Round 2 — preflight-and-advise hook surfaces the re-trust note in
          additionalContext when trust_state="stale".
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PASS, FAIL = [], []
PLUGIN_ROOT = Path("/Users/crisn/Documents/Projects/chameleon")


def t(name, condition, info=""):
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' — ' + info) if info else ''}")


def section(title):
    print(f"\n=== {title} ===")


from chameleon_mcp.tools import (
    _compute_repo_id, bootstrap_repo, detect_repo, get_pattern_context,
    trust_profile,
)
from chameleon_mcp.profile.trust import (
    is_material_change, revoke_trust, trust_state_for,
)


def make_synthetic_repo(root: Path) -> None:
    (root / "src" / "utils").mkdir(parents=True)
    (root / "tsconfig.json").write_text('{}')
    (root / "package.json").write_text('{"name":"x","dependencies":{"typescript":"5.0.0"}}')
    for i in range(6):
        (root / "src" / "utils" / f"f{i}.ts").write_text(f"export const f{i} = {i};\n")


# Use synthetic repos so we don't perturb test repo state
with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp) / "material_test"
    repo.mkdir()
    make_synthetic_repo(repo)

    # ----------------------------------------------------------------------
    # Round 1 — detect_repo + get_pattern_context return "stale" correctly
    # ----------------------------------------------------------------------
    section("Round 1 — detect_repo trust_state transitions")

    # Bootstrap
    bootstrap_repo(str(repo))
    rid = _compute_repo_id(repo)
    sample = repo / "src" / "utils" / "f0.ts"

    # No trust → untrusted
    revoke_trust(rid)
    r = detect_repo(str(sample))["data"]
    t("Before trust grant: trust_state=untrusted", r["trust_state"] == "untrusted")

    # Grant trust
    trust_profile(str(repo), repo.name)
    r = detect_repo(str(sample))["data"]
    t("After trust grant: trust_state=trusted", r["trust_state"] == "trusted")

    # Mutate the profile (simulate teammate edit). Use a non-generation
    # field so the loader's cross-file generation check still passes —
    # we want to test material-change detection, not loader rejection.
    profile_path = repo / ".chameleon" / "profile.json"
    profile = json.loads(profile_path.read_text())
    profile["created_at"] = "2099-01-01T00:00:00Z"
    profile_path.write_text(json.dumps(profile, indent=2, sort_keys=True))

    # Now is_material_change should return True
    t("is_material_change returns True after profile mutated",
      is_material_change(rid, repo / ".chameleon"))

    # detect_repo should return "stale"
    r = detect_repo(str(sample))["data"]
    t(f"After mutation: trust_state=stale (got {r['trust_state']})",
      r["trust_state"] == "stale")

    # get_pattern_context also returns "stale"
    r = get_pattern_context(str(sample))["data"]
    t(f"get_pattern_context also returns stale (got {r['repo']['trust_state']})",
      r["repo"]["trust_state"] == "stale")

    # Re-grant trust → trusted again
    trust_profile(str(repo), repo.name)
    r = detect_repo(str(sample))["data"]
    t("After re-trust: trust_state=trusted again", r["trust_state"] == "trusted")


# ---------------------------------------------------------------------------
# Round 2 — preflight-and-advise surfaces re-trust hint when stale
# ---------------------------------------------------------------------------
section("Round 2 — preflight surfaces re-trust hint when stale")

with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp) / "preflight_stale"
    repo.mkdir()
    make_synthetic_repo(repo)
    bootstrap_repo(str(repo))
    rid = _compute_repo_id(repo)
    trust_profile(str(repo), repo.name)

    # Mutate profile to make trust stale (non-generation field so loader
    # doesn't reject)
    profile_path = repo / ".chameleon" / "profile.json"
    profile = json.loads(profile_path.read_text())
    profile["created_at"] = "2099-01-01T00:00:00Z"
    profile_path.write_text(json.dumps(profile, indent=2, sort_keys=True))

    sample = repo / "src" / "utils" / "f0.ts"
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
    payload = json.dumps({
        "tool_name": "Edit",
        "tool_input": {"file_path": str(sample)},
        "session_id": "stale-test",
    })
    proc = subprocess.run(
        [str(PLUGIN_ROOT / "hooks" / "preflight-and-advise")],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    out = json.loads(proc.stdout) if proc.stdout.strip() else {}
    ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    t(
        "Stale trust: hook still emits archetype context",
        "[chameleon: archetype=" in ctx,
    )
    t(
        "Stale trust: hook surfaces 'Trust is stale' note",
        "Trust is stale" in ctx,
    )
    t(
        "Stale trust: hook suggests /chameleon-trust to re-confirm",
        "/chameleon-trust" in ctx,
    )

    # After re-trust, the note disappears
    trust_profile(str(repo), repo.name)
    proc = subprocess.run(
        [str(PLUGIN_ROOT / "hooks" / "preflight-and-advise")],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    out = json.loads(proc.stdout) if proc.stdout.strip() else {}
    ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    t(
        "After re-trust: stale note no longer present",
        "Trust is stale" not in ctx,
    )


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
