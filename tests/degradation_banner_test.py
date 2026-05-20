"""Tests for rec 3: unified degradation banner.

Covers:
- _degraded_banner produces a well-formed <chameleon-context> envelope.
- preflight-and-advise emits a banner when get_pattern_context raises
  (previously silent fail_open).
- The historical "session_disable" mislabel on the trust-prompt-dedup
  path is fixed to "trust_prompt_dedup".
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import chameleon_mcp.hook_helper as hh
import chameleon_mcp.tools as tools_mod
from chameleon_mcp.hook_helper import _degraded_banner

PASS: list[tuple[str, str]] = []
FAIL: list[tuple[str, str]] = []


def t(name: str, condition: bool, info: str = "") -> None:
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' - ' + info) if info else ''}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


REPO_ROOT = Path(__file__).resolve().parent.parent


section("_degraded_banner shape")
banner = _degraded_banner("advisor_unavailable", "edit proceeds without guidance")
t("opens <chameleon-context>", banner.startswith("<chameleon-context>"))
t("closes </chameleon-context>", banner.endswith("</chameleon-context>"))
t(
    "names the reason in the bracketed header",
    "[chameleon: degraded — advisor_unavailable]" in banner,
)
t("contains the detail line", "edit proceeds without guidance" in banner)

bare = _degraded_banner("mcp_timeout")
t("works without detail", "[chameleon: degraded — mcp_timeout]" in bare)


section("preflight-and-advise emits banner when get_pattern_context raises")
# In-process test: monkey-patch chameleon_mcp.tools.get_pattern_context
# to raise, then call preflight_and_advise() directly. The subprocess
# approach with a corrupt profile.json is defended inside
# get_pattern_context itself (returns profile_corrupted envelope, never
# raises), so it doesn't reach the new banner path. Forcing a raise is
# the only way to exercise the rec 3 surface.
with tempfile.TemporaryDirectory() as td:
    repo = Path(td)
    src = repo / "main.ts"
    src.write_text("export const x = 1;\n", encoding="utf-8")
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(src)},
        "session_id": "deg-test-2",
    }

    def _boom(*_a, **_kw):
        raise RuntimeError("simulated advisor crash")

    # The hook reads payload from sys.stdin and writes to sys.stdout.
    # Also force the daemon path to miss so the in-process branch fires.
    buf_out = io.StringIO()
    stdin_replay = io.StringIO(json.dumps(payload))

    # Patch both paths get_pattern_context can be reached through: the
    # in-process import inside preflight_and_advise() and the daemon
    # client (so it returns None and falls through to the in-process
    # branch where _boom raises).
    import chameleon_mcp.daemon_client as dc

    with (
        patch.object(tools_mod, "get_pattern_context", _boom),
        patch.object(dc, "call", lambda *_a, **_kw: None),
        patch.object(sys, "stdin", stdin_replay),
        redirect_stdout(buf_out),
    ):
        rc = hh.preflight_and_advise()
    out = buf_out.getvalue().strip()
    parsed = json.loads(out) if out else {}
    ctx = parsed.get("hookSpecificOutput", {}).get("additionalContext", "")
    t("preflight exits 0 on raised advisor", rc == 0, f"rc={rc}")
    t("output is a chameleon-context block", ctx.startswith("<chameleon-context>"))
    t(
        "header names degradation reason",
        "[chameleon: degraded — advisor_unavailable]" in ctx,
        ctx[:120],
    )
    t(
        "detail line names /chameleon-doctor",
        "/chameleon-doctor" in ctx,
    )


section("suppression_reason relabel: trust_prompt_dedup not session_disable")
source = Path(hh.__file__).read_text(encoding="utf-8")
t(
    'no remaining "session_disable" mislabel on the trust-prompt-dedup branch',
    'suppression_reason="trust_prompt_dedup"' in source,
)
stale_count = source.count('suppression_reason="session_disable"')
t(
    "session_disable label remains reachable only via genuine /chameleon-disable",
    # The label is fine in the suppression-check helper, but must not
    # appear inside the trust-prompt-dedup else-branch any more.
    stale_count == 0,
    f"count={stale_count}",
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
