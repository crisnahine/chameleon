"""Verify all 4 opt-out mechanisms actually suppress preflight injection.

Round 1: unit-level — write each opt-out marker, run preflight-and-advise
         hook bash script with a a real test file, verify the response is {}
         (no <chameleon-context> injected) instead of the normal archetype
         block.
Round 2: end-to-end via the MCP tools — call disable_session and
         pause_session through the MCP API, verify markers land in the
         right place and is_chameleon_suppressed returns the expected
         reason.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from _test_config import TS_REPO

PASS, FAIL = [], []
PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def t(name, condition, info=""):
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' — ' + info) if info else ''}")


def section(title):
    print(f"\n=== {title} ===")


from chameleon_mcp.tools import (
    bootstrap_repo, disable_session, pause_session, trust_profile,
)
from chameleon_mcp.optouts import (
    clear_pause, clear_session_disable, is_chameleon_suppressed,
    write_pause, write_session_disable,
)
from chameleon_mcp.profile.trust import repo_data_dir


# Ensure the TypeScript repo is bootstrapped + trusted
if not (TS_REPO / ".chameleon" / "profile.json").is_file():
    bootstrap_repo(str(TS_REPO))
trust_profile(str(TS_REPO), "client")
client_repo_id = hashlib.sha256(str(TS_REPO.resolve()).encode("utf-8")).hexdigest()


def run_preflight(file_path: str, session_id: str = "optouts-test") -> dict:
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
    payload = json.dumps({
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path},
        "session_id": session_id,
    })
    proc = subprocess.run(
        [str(PLUGIN_ROOT / "hooks" / "preflight-and-advise")],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


sample_ts = str(TS_REPO / "src" / "components" / "base" / "SelectVettingStatus.tsx")


# ---------------------------------------------------------------------------
# Baseline — preflight injects context normally (no opt-outs active)
# ---------------------------------------------------------------------------
section("Baseline — no opt-outs, normal injection")

# Clear any leftover state
clear_pause(client_repo_id)
clear_session_disable(client_repo_id, "optouts-test")
os.environ.pop("CHAMELEON_DISABLE", None)
(TS_REPO / ".chameleon" / ".skip").unlink(missing_ok=True)

baseline = run_preflight(sample_ts)
ctx = baseline.get("hookSpecificOutput", {}).get("additionalContext", "")
t("Baseline: archetype context injected", "[chameleon: archetype=" in ctx)


# ---------------------------------------------------------------------------
# Round 1 — each opt-out suppresses injection
# ---------------------------------------------------------------------------
section("Round 1 — .chameleon/.skip suppresses injection")

skip_path = TS_REPO / ".chameleon" / ".skip"
skip_path.write_text("acceptance test")
try:
    out = run_preflight(sample_ts)
    t("Repo .skip → empty hook response", out == {})
finally:
    skip_path.unlink(missing_ok=True)


section("Round 1 — CHAMELEON_DISABLE=1 suppresses injection")

os.environ["CHAMELEON_DISABLE"] = "1"
try:
    out = run_preflight(sample_ts)
    t("CHAMELEON_DISABLE=1 → empty hook response", out == {})
finally:
    del os.environ["CHAMELEON_DISABLE"]


section("Round 1 — session-scoped disable suppresses injection")

write_session_disable(client_repo_id, "optouts-test")
try:
    out = run_preflight(sample_ts, session_id="optouts-test")
    t("session_disable marker → empty hook response", out == {})

    # Verify a DIFFERENT session_id is NOT suppressed
    other = run_preflight(sample_ts, session_id="other-session")
    other_ctx = other.get("hookSpecificOutput", {}).get("additionalContext", "")
    t(
        "Different session unaffected",
        "[chameleon: archetype=" in other_ctx,
    )
finally:
    clear_session_disable(client_repo_id, "optouts-test")


section("Round 1 — pause suppresses injection until expiry")

# Pause 15 min in future
expiry_iso = write_pause(client_repo_id, minutes=15)
try:
    out = run_preflight(sample_ts)
    t("pause_until in future → empty hook response", out == {})

    # Pause in PAST should NOT suppress + should auto-clean
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pause_path = repo_data_dir(client_repo_id) / ".pause_until"
    pause_path.write_text(past)
    out = run_preflight(sample_ts)
    ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    t("pause_until in past → injection resumes", "[chameleon: archetype=" in ctx)
    t("Expired pause file auto-cleaned", not pause_path.is_file())
finally:
    clear_pause(client_repo_id)


# ---------------------------------------------------------------------------
# Round 1 — is_chameleon_suppressed returns the right reason
# ---------------------------------------------------------------------------
section("Round 1 — is_chameleon_suppressed reasons")

t(
    "No opt-outs: returns None",
    is_chameleon_suppressed(TS_REPO, client_repo_id, "x") is None,
)

skip_path.write_text("x")
t(
    ".skip: returns 'repo_skip'",
    is_chameleon_suppressed(TS_REPO, client_repo_id, "x") == "repo_skip",
)
skip_path.unlink()

os.environ["CHAMELEON_DISABLE"] = "1"
t(
    "CHAMELEON_DISABLE: returns 'user_disable'",
    is_chameleon_suppressed(TS_REPO, client_repo_id, "x") == "user_disable",
)
del os.environ["CHAMELEON_DISABLE"]

write_session_disable(client_repo_id, "x")
t(
    "session marker: returns 'session_disable'",
    is_chameleon_suppressed(TS_REPO, client_repo_id, "x") == "session_disable",
)
clear_session_disable(client_repo_id, "x")

write_pause(client_repo_id, minutes=15)
t(
    "pause marker: returns 'pause'",
    is_chameleon_suppressed(TS_REPO, client_repo_id, "x") == "pause",
)
clear_pause(client_repo_id)


# ---------------------------------------------------------------------------
# Round 2 — disable_session and pause_session MCP tools
# ---------------------------------------------------------------------------
section("Round 2 — MCP tools write the right markers")

r = disable_session(str(TS_REPO), "tool-test-session")
t("disable_session returns success", r["data"]["status"] == "success")
marker = repo_data_dir(client_repo_id) / ".session_disabled.tool-test-session"
t("disable_session writes the marker", marker.is_file())
clear_session_disable(client_repo_id, "tool-test-session")

r = pause_session(str(TS_REPO), minutes=10)
t("pause_session returns success", r["data"]["status"] == "success")
t("pause_session response has expires_at", "expires_at" in r["data"])
pause_path = repo_data_dir(client_repo_id) / ".pause_until"
t("pause_session writes the marker", pause_path.is_file())
clear_pause(client_repo_id)


# ---------------------------------------------------------------------------
# Round 2 — invalid args rejected
# ---------------------------------------------------------------------------
section("Round 2 — disable/pause invalid args")

r = disable_session("not/an/abs/path", "x")
t("disable_session: relative path rejected", r["data"]["status"] == "failed")

r = disable_session(str(TS_REPO), "")
t("disable_session: empty session_id rejected", r["data"]["status"] == "failed")

r = pause_session(str(TS_REPO), minutes=0)
t("pause_session: minutes=0 rejected", r["data"]["status"] == "failed")

r = pause_session(str(TS_REPO), minutes=999)
t("pause_session: minutes=999 rejected", r["data"]["status"] == "failed")


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
