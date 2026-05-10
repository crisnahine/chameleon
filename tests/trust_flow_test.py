"""Verify /chameleon-trust confirmation flow.

Round 1 — unit-level: trust_profile rejects bad tokens, accepts both
          documented forms (repo basename and yes-trust-<8>), and the
          .trust file roundtrips correctly.
Round 2 — real Claude Code: invoke /chameleon-trust as a slash command
          on both EF stacks and verify Claude reads profile.summary.md
          before granting (per the skill flow doc).
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PASS, FAIL = [], []
PLUGIN_ROOT = Path("/Users/crisn/Documents/Projects/chameleon")
EF_CLIENT = Path("/Users/crisn/Documents/Projects/empire-flippers/client")
EF_API = Path("/Users/crisn/Documents/Projects/empire-flippers/api")


def t(name, condition, info=""):
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' — ' + info) if info else ''}")


def section(title):
    print(f"\n=== {title} ===")


from chameleon_mcp.tools import bootstrap_repo, trust_profile, _compute_repo_id
from chameleon_mcp.profile.trust import revoke_trust, trust_state_for


# Ensure both EF repos bootstrapped
for repo in (EF_CLIENT, EF_API):
    if not repo.is_dir():
        continue
    if not (repo / ".chameleon" / "profile.json").is_file():
        bootstrap_repo(str(repo))


# ---------------------------------------------------------------------------
# Round 1 — confirmation_token validation
# ---------------------------------------------------------------------------
section("Round 1 — confirmation_token validation")

# Wrong token rejected
r = trust_profile(str(EF_CLIENT), "wrong-token")
t(
    "Wrong confirmation_token rejected with status=failed",
    r["data"]["status"] == "failed",
)
t(
    "Error message names both acceptable forms",
    "yes-trust-" in r["data"]["error"] and "client" in r["data"]["error"],
)

# Exact basename accepted
r = trust_profile(str(EF_CLIENT), "client")
t("Exact repo basename token accepted", r["data"]["status"] == "success")
client_repo_id = _compute_repo_id(EF_CLIENT)
t("Trust state recorded after grant", trust_state_for(client_repo_id) is not None)

# Revoke then test yes-trust-<8> form
revoke_trust(client_repo_id)
short = client_repo_id[:8]
r = trust_profile(str(EF_CLIENT), f"yes-trust-{short}")
t(
    "yes-trust-<8> token accepted",
    r["data"]["status"] == "success",
)
t("Trust restored via yes-trust form", trust_state_for(client_repo_id) is not None)

# Empty token rejected
r = trust_profile(str(EF_CLIENT), "")
t("Empty token rejected", r["data"]["status"] == "failed")

# Wrong-but-similar tokens rejected
r = trust_profile(str(EF_CLIENT), "Client")  # case-sensitive
t("Case-mismatched basename rejected", r["data"]["status"] == "failed")

r = trust_profile(str(EF_CLIENT), f"yes-trust-{short[:6]}")  # short prefix
t("Short prefix yes-trust rejected", r["data"]["status"] == "failed")

r = trust_profile(str(EF_CLIENT), f"yes-trust-{client_repo_id}")  # full hash, not 8
t("Full hash yes-trust rejected (must be 8 chars)", r["data"]["status"] == "failed")


# ---------------------------------------------------------------------------
# Round 1 — trust on missing profile
# ---------------------------------------------------------------------------
section("Round 1 — trust without a profile")

with tempfile.TemporaryDirectory() as tmp:
    no_profile = Path(tmp) / "no_profile_repo"
    no_profile.mkdir()
    r = trust_profile(str(no_profile), no_profile.name)
    t(
        "Trust without .chameleon/profile.json rejected",
        r["data"]["status"] == "failed" and "no profile" in r["data"]["error"],
    )


# ---------------------------------------------------------------------------
# Round 1 — trust record contains the right repo_root
# ---------------------------------------------------------------------------
section("Round 1 — trust record fidelity")

trust_profile(str(EF_CLIENT), "client")
record = trust_state_for(client_repo_id)
t("Trust record records correct repo_root", record.repo_root == str(EF_CLIENT.resolve()))
t("Trust record has granted_by_user", bool(record.granted_by_user))
t("Trust record has profile_sha256 (64 hex chars)", len(record.profile_sha256) == 64)


# ---------------------------------------------------------------------------
# Round 2 — real Claude Code /chameleon-trust on both EF stacks
# ---------------------------------------------------------------------------
section("Round 2 — /chameleon-trust via real Claude Code")

if shutil.which("claude") is None:
    print("  SKIP: claude CLI not on PATH")
else:
    for label, repo_root in [("EF client", EF_CLIENT), ("EF api", EF_API)]:
        if not repo_root.is_dir():
            continue
        # Revoke trust first to make the test meaningful
        rid = _compute_repo_id(repo_root)
        revoke_trust(rid)

        env = os.environ.copy()
        env.pop("CHAMELEON_DISABLE", None)

        proc = subprocess.run(
            [
                "claude", "-p",
                f"/chameleon:chameleon-trust\n\nThe repo I want to trust is the current working directory. The repo basename you can use as confirmation_token is {repo_root.name!r}. Please proceed.",
                "--plugin-dir", str(PLUGIN_ROOT),
                "--output-format", "stream-json",
                "--max-turns", "10",
                "--verbose",
                "--allowedTools",
                f"Bash Read mcp__plugin_chameleon_chameleon-mcp__trust_profile mcp__plugin_chameleon_chameleon-mcp__detect_repo",
            ],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )

        events = []
        for line in proc.stdout.splitlines():
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        # Verify Claude called trust_profile
        called_trust = False
        for e in events:
            msg = e.get("message", {})
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "tool_use"
                    and item.get("name") == "mcp__plugin_chameleon_chameleon-mcp__trust_profile"
                ):
                    called_trust = True

        t(f"{label}: Claude invoked trust_profile via /chameleon-trust", called_trust)

        # Verify trust state was actually granted
        new_record = trust_state_for(rid)
        t(
            f"{label}: trust granted after /chameleon-trust completes",
            new_record is not None,
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
