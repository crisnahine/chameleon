"""Pin the v0.5.14 bug-8 fix: disable_session marker is HMAC-signed.

Bug: disable_session(repo, session_id="fake") succeeded and wrote a
marker. A third-party process that learned another user's session_id
could pre-write a disable marker to silently suppress chameleon's
advisories for that user.

Fix: write_session_disable now HMAC-signs the marker with the local
HMAC key (the same key the exec_log uses). is_chameleon_suppressed
verifies the signature before honoring the marker. Markers without a
signature are still honored for back-compat (v0.5.13 markers and
systems where the HMAC key is unavailable). Only PRESENT-BUT-WRONG
signatures are rejected — i.e. a third-party process that planted
a forged marker without the key.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import os
import sys
import tempfile
import time

_TMP_PD = tempfile.TemporaryDirectory()
os.environ["CHAMELEON_PLUGIN_DATA"] = _TMP_PD.name

from chameleon_mcp.exec_log import _ensure_hmac_key  # noqa: E402
from chameleon_mcp.optouts import (  # noqa: E402
    _safe_session_marker,
    is_chameleon_suppressed,
    write_session_disable,
)
from chameleon_mcp.profile.trust import repo_data_dir  # noqa: E402

PASS: list[tuple[str, str]] = []
FAIL: list[tuple[str, str]] = []


def t(name: str, condition: bool, info: str = "") -> None:
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' - ' + info) if info else ''}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


REPO_ID = "test_repo_id_for_hmac_session"
SESSION_ID = "sess-1234"

section("legitimate disable: marker written + honored")
marker = write_session_disable(REPO_ID, SESSION_ID)
t("marker file created", marker.is_file(), str(marker))
text = marker.read_text(encoding="utf-8")
t("marker contains HMAC signature line", "sig=" in text, text[:120])
reason = is_chameleon_suppressed(None, REPO_ID, SESSION_ID)
t(
    "is_chameleon_suppressed honors valid marker",
    reason == "session_disable",
    repr(reason),
)
# Cleanup
marker.unlink(missing_ok=True)


section("DOWNGRADE-ATTACK: attacker writes unsigned marker — REJECTED (review finding)")
# An attacker who learned a session_id could pre-write a marker with
# NO sig= line, exploiting the original back-compat branch. Post-fix:
# when the local HMAC key is available, unsigned markers are REJECTED.
marker = (
    repo_data_dir(REPO_ID) / f".session_disabled.{_safe_session_marker(SESSION_ID)}"
)
marker.write_text(
    f"disabled-at={time.time()}\nsession_id={SESSION_ID}\n", encoding="utf-8"
)
reason = is_chameleon_suppressed(None, REPO_ID, SESSION_ID)
t(
    "unsigned marker is REJECTED (closes downgrade attack)",
    reason is None,
    f"got reason={reason!r}",
)
marker.unlink(missing_ok=True)


section("FORGED marker with WRONG signature — REJECTED (this is the bug-8 fix)")
marker.write_text(
    f"disabled-at={time.time()}\nsession_id={SESSION_ID}\n"
    f"sig=0000000000000000000000000000000000000000000000000000000000000000\n",
    encoding="utf-8",
)
reason = is_chameleon_suppressed(None, REPO_ID, SESSION_ID)
t(
    "marker with bad signature is REJECTED",
    reason is None,
    f"got reason={reason!r}",
)
marker.unlink(missing_ok=True)


section("attacker who knows the HMAC key produces a valid marker")
# This is the threat model boundary: an attacker WITH the key can
# forge markers — which means HMAC-signing doesn't help against a
# privileged attacker, only against an unprivileged session-id leak.
# This test documents the boundary.
key = _ensure_hmac_key()
attacker_disabled_at = time.time()
attacker_sig = _hmac.new(
    key,
    f"{REPO_ID}|{SESSION_ID}|{attacker_disabled_at}".encode(),
    hashlib.sha256,
).hexdigest()
marker.write_text(
    f"disabled-at={attacker_disabled_at}\nsession_id={SESSION_ID}\nsig={attacker_sig}\n",
    encoding="utf-8",
)
reason = is_chameleon_suppressed(None, REPO_ID, SESSION_ID)
t(
    "marker with valid signature IS honored (regardless of who wrote it)",
    reason == "session_disable",
    repr(reason),
)
marker.unlink(missing_ok=True)


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
